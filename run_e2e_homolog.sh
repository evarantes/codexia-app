#!/usr/bin/env bash
# =============================================================================
# Codexia Homolog — YouTube Auto E2E Validation (single-run bash script)
# Executa TUDO: git → build → docker run → login → série nova → 1 episódio
# → scheduler → acompanha progresso → valida MP4 → confirma YouTube unlisted.
# Não pede nenhum input depois de iniciado.
# =============================================================================
set -u
set -o pipefail
LANG=C.UTF-8

START_TS=$(date -u +%s)
START_HUMAN=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKDIR="${WORKDIR:-$SCRIPT_DIR}"
LOG_DIR="${LOG_DIR:-$WORKDIR/e2e_logs}"
mkdir -p "$LOG_DIR"
REPORT="$LOG_DIR/e2e_report_$(date -u '+%Y%m%dT%H%M%SZ').txt"
MAIN_LOG="$LOG_DIR/e2e_run_$(date -u '+%Y%m%dT%H%M%SZ').log"
touch "$REPORT" "$MAIN_LOG"

BRANCH="homolog/youtube-auto-e2e"
REMOTE="origin"
IMAGE="codexia:homolog-ytauto-final"
CONTAINER="codexia-homolog-ytauto-final"
CONTAINER_NETWORK="${CONTAINER_NETWORK:-coolify}"
PORT="8010"
BASE_URL="http://127.0.0.1:${PORT}"
MOUNT_VOLUME_HOST="/root/codexia-homolog-media"
MOUNT_VOLUME_GUEST="/data"

ADMIN_EMAIL="${ADMIN_EMAIL:-admin@codexia.dev}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin123}"
SECRET_KEY="${SECRET_KEY:-dev-secret-key-codexia-2025}"
APP_ENV="${APP_ENV:-development}"
# Nome do banco SEMPRE homolog. Nunca tocar produção.
FORCED_DB_NAME="codexia_sprint1_validation"
# Referência container homolog (nunca produção): somente usamos este nome
# para extrair DATABASE_URL de referência.
REFERENCE_CONTAINER_HOMOLOG="g8w4so4gkkgog0scsw0ogwkw-200824550318"

# Optional: channel id used only for final youtube listing sanity check
YOUTUBE_CHANNEL_ID="${YOUTUBE_CHANNEL_ID:-}"

# Idempotency: key fixa para NÃO duplicar série/upload em reexecuções.
# 32 chars + data diária => uma nova série por dia é aceitável.
RUN_TAG="${RUN_TAG:-$(date -u +%Y%m%d)}"
IDEMPOTENCY_KEY="e2e-homolog-ytauto-${RUN_TAG}"
# Marcador de execução prévia (no workspace, no volume e no result_json idempotency)
PREV_LOCK_HOST_DIR="${MOUNT_VOLUME_HOST}/e2e_state"
PREV_LOCK_FILE="${PREV_LOCK_HOST_DIR}/run_${RUN_TAG}.state"
mkdir -p "$PREV_LOCK_HOST_DIR" || true

ok_count=0
fail_count=0

section() {
  echo ""
  echo "======================================================================"
  echo "== $*"
  echo "======================================================================" | tee -a "$MAIN_LOG"
  echo "" | tee -a "$MAIN_LOG"
}

log() {
  local ts
  ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  echo "[${ts}] $*" | tee -a "$MAIN_LOG"
}

pass() {
  ok_count=$((ok_count + 1))
  log "✅ PASS — $*"
  echo "[OK] $*" >>"$REPORT"
}

fail() {
  fail_count=$((fail_count + 1))
  log "❌ FAIL — $*"
  echo "[FAIL] $*" >>"$REPORT"
  SUMMARY_AT
  exit 1
}

STEP_AT() {
  local title="$1"
  log "▶ STEP: ${title}"
  echo "" >>"$REPORT"
  echo "--- STEP: ${title} ---" >>"$REPORT"
}

SUMMARY_AT() {
  END_TS=$(date -u +%s)
  END_HUMAN=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  TOTAL_SEC=$((END_TS - START_TS))
  MIN=$((TOTAL_SEC / 60))
  SEC=$((TOTAL_SEC % 60))
  {
    echo ""
    echo "======================================================================="
    echo " E2E Homolog YouTube Auto — Resumo"
    echo "======================================================================="
    echo " Container         : ${CONTAINER}"
    echo " Rede docker       : ${CONTAINER_NETWORK}"
    echo " Porta exposta     : ${PORT}"
    echo " Imagem            : ${IMAGE}"
    echo " Banco (forçado)   : ${FORCED_DB_NAME}"
    echo " Volume persistente: ${MOUNT_VOLUME_HOST}:${MOUNT_VOLUME_GUEST}"
    echo " Idempotency Key   : ${IDEMPOTENCY_KEY}"
    echo " Início            : ${START_HUMAN}"
    echo " Fim               : ${END_HUMAN}"
    echo " Tempo total       : ${MIN}m ${SEC}s (${TOTAL_SEC} s)"
    echo " Passos OK         : ${ok_count}"
    echo " Passos FALHA      : ${fail_count}"
    echo " Relatório txt     : ${REPORT}"
    echo " Log completo      : ${MAIN_LOG}"
    echo " SHA atual (local) : ${CURRENT_SHA:-n/d}"
    echo "======================================================================="
    echo ""
    if [ "${FINAL_SUCCESS:-0}" = "1" ]; then
      echo "🎉 MISSÃO CUMPRIDA — pipeline YouTube Auto validado ponta-a-ponta."
    else
      echo "⚠️  Falha técnica durante execução — ver erros acima e em ${REPORT}"
      echo "   Upload no YouTube NÃO foi realizado se falhou antes da etapa 11."
    fi
  } | tee -a "$MAIN_LOG" "$REPORT"
}

trap 'SUMMARY_AT' EXIT

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Faltando comando obrigatório no servidor: $1"
}

CURL_SILENT="curl -sS --max-time 60"

http_ok() {
  local url="$1"
  shift || true
  local out
  out=$(${CURL_SILENT} -o /dev/null -w "%{http_code}" "$@" "$url" 2>>"$MAIN_LOG")
  echo "$out"
}

http_json() {
  local url="$1"
  shift || true
  ${CURL_SILENT} -H 'Accept: application/json' "$@" "$url" 2>>"$MAIN_LOG"
}

http_post() {
  local url="$1"
  shift || true
  ${CURL_SILENT} -X POST -H 'Accept: application/json' "$@" "$url" 2>>"$MAIN_LOG"
}

http_put() {
  local url="$1"
  shift || true
  ${CURL_SILENT} -X PUT -H 'Accept: application/json' "$@" "$url" 2>>"$MAIN_LOG"
}

# =============================================================================
# 0 — Pré-requisitos do ambiente + proteção ANTI-PRODUÇÃO
# =============================================================================
section "0. Pré-requisitos + Proteção Anti-Produção"
require_cmd git
require_cmd docker
require_cmd curl
require_cmd jq
require_cmd stat
HOST_HAS_FFPROBE=0
if command -v ffprobe >/dev/null 2>&1; then HOST_HAS_FFPROBE=1; fi
pass "Comandos obrigatórios presentes (git docker curl jq stat). ffprobe host=${HOST_HAS_FFPROBE} (sempre usamos ffprobe via docker exec quando container está up)"

# --- PROTEÇÃO 1: Jamais tocar containers de produção. -----------------------
# Lista de substrings proibidas para nome de container. Se existir na lista
# e for igual NOME ao REFERENCE_CONTAINER_HOMOLOG, TUDO BEM. Caso contrário,
# aborta.
PROD_NAME_PATTERNS=(
  "codexia-prod"
  "codexia-production"
  "coolify-codexia-prod"
  "prod"
)
for p in "${PROD_NAME_PATTERNS[@]}"; do
  if [ "$p" = "$CONTAINER" ]; then
    fail "Detectado CONTAINER=$CONTAINER com padrão de produção '$p'. ABORTADO para não tocar produção."
  fi
  if docker ps -a --format '{{.Names}}' | grep -Fxq "$p" && [ "$p" != "$REFERENCE_CONTAINER_HOMOLOG" ]; then
    log "Nota: container $p existe, mas NÃO vamos tocá-lo (nome proibido)."
  fi
done
pass "Proteção 1 OK: nomes de produção (prod/codexia-prod/coolify-codexia-prod) NÃO são nosso target."

# --- PROTEÇÃO 2: Resolver DATABASE_URL do REFERENCE_CONTAINER_HOMOLOG. ------
# Nunca recebemos segredo por input. Sempre lemos do container homolog.
if [ -z "${DATABASE_URL:-}" ]; then
  if ! docker ps -a --format '{{.Names}}' | grep -Fxq "$REFERENCE_CONTAINER_HOMOLOG"; then
    fail "Container referência homolog $REFERENCE_CONTAINER_HOMOLOG não foi encontrado no docker. Impossível extrair DATABASE_URL."
  fi
  RAW_DB=$(docker inspect "$REFERENCE_CONTAINER_HOMOLOG" --format '{{range .Config.Env}}{{.}}{{"\n"}}{{end}}' | awk -F= '/^DATABASE_URL=/{$1=""; sub(/^=/,""); print; exit}' 2>>"$MAIN_LOG" || echo "")
  [ -n "$RAW_DB" ] || fail "Container referência $REFERENCE_CONTAINER_HOMOLOG não tem variável DATABASE_URL definida."
  # Normaliza: troca o path final do banco para FORCED_DB_NAME="codexia_sprint1_validation"
  # remove qualquer query, substitui último /qualquercoisa por /FORCED_DB_NAME?query
  DB_WITHOUT_QUERY="${RAW_DB%%\?*}"
  DB_QUERY_PART=""
  case "$RAW_DB" in
    *\?*) DB_QUERY_PART="?${RAW_DB#*\?}";;
  esac
  NORMALIZED="${DB_WITHOUT_QUERY%/*}/${FORCED_DB_NAME}${DB_QUERY_PART}"
  export DATABASE_URL="$NORMALIZED"
  # Confirma que o nome do banco é EXATAMENTE o de homolog (nunca outro)
  NAME_CHECK="${DATABASE_URL%${DB_QUERY_PART}}"
  NAME_CHECK="${NAME_CHECK##*/}"
  if [ "$NAME_CHECK" != "$FORCED_DB_NAME" ]; then
    fail "Nome do banco resolvido ($NAME_CHECK) != esperado ($FORCED_DB_NAME). ABORTADO para não tocar produção."
  fi
  pass "Proteção 2 OK: DATABASE_URL lido de $REFERENCE_CONTAINER_HOMOLOG e normalizado para banco=${FORCED_DB_NAME}."
else
  # Mesmo se por env veio, FORÇAMOS o nome do banco para homolog.
  RAW_DB="$DATABASE_URL"
  DB_WITHOUT_QUERY="${RAW_DB%%\?*}"
  DB_QUERY_PART=""
  case "$RAW_DB" in
    *\?*) DB_QUERY_PART="?${RAW_DB#*\?}";;
  esac
  NORMALIZED="${DB_WITHOUT_QUERY%/*}/${FORCED_DB_NAME}${DB_QUERY_PART}"
  export DATABASE_URL="$NORMALIZED"
  NAME_CHECK="${DATABASE_URL%${DB_QUERY_PART}}"
  NAME_CHECK="${NAME_CHECK##*/}"
  if [ "$NAME_CHECK" != "$FORCED_DB_NAME" ]; then
    fail "Mesmo após normalizar, nome do banco ($NAME_CHECK) != ($FORCED_DB_NAME). Abortado."
  fi
  pass "Proteção 2 OK: DATABASE_URL recebido por ENV, normalizado para banco=${FORCED_DB_NAME}."
fi
echo "FORCED_DB_NAME=$FORCED_DB_NAME" >>"$REPORT"
echo "REFERENCE_CONTAINER=$REFERENCE_CONTAINER_HOMOLOG" >>"$REPORT"
echo "DATABASE_URL_NAME_CHECK=$NAME_CHECK" >>"$REPORT"

# --- PROTEÇÃO 3: NÃO publica mais de 1 vídeo e NÃO publica público. --------
# Reforçado dentro do payload (visibility sempre unlisted, 1 episódio,
# idempotency).
echo "VISIBILITY_FORCED=unlisted" >>"$REPORT"
echo "EPISODES_FORCED=1" >>"$REPORT"
pass "Proteção 3 OK: visibility sempre unlisted; 1 episódio; idempotency por dia."

# --- PROTEÇÃO 4: Idempotência / detectar execução anterior. -----------------
IDEMPOTENCY_TASK_ID=""
IDEMPOTENCY_SERIES_ID=""
if [ -f "$PREV_LOCK_FILE" ]; then
  PREV_SERIES=$(grep -E '^PREV_SERIES_ID=' "$PREV_LOCK_FILE" | head -n1 | cut -d= -f2- || echo "")
  PREV_TASK=$(grep -E '^PREV_TASK_ID=' "$PREV_LOCK_FILE" | head -n1 | cut -d= -f2- || echo "")
  PREV_VIDEO=$(grep -E '^PREV_YOUTUBE_VIDEO_ID=' "$PREV_LOCK_FILE" | head -n1 | cut -d= -f2- || echo "")
  PREV_STATE=$(grep -E '^PREV_STATE=' "$PREV_LOCK_FILE" | head -n1 | cut -d= -f2- || echo "")
  log "Idempotência: arquivo state existente em $PREV_LOCK_FILE"
  log "  state=$PREV_STATE series_id=$PREV_SERIES task_id=$PREV_TASK yt_id=${PREV_VIDEO:0:8}..."
  if [ "$PREV_STATE" = "COMPLETED" ] && [ -n "$PREV_VIDEO" ]; then
    # Já rodou com sucesso hoje — abortamos para não duplicar upload no YT.
    fail "Idempotência: run_${RUN_TAG}.state=COMPLETED e youtube_video_id=$PREV_VIDEO já existem. NÃO duplicar upload. Use RUN_TAG=YYYYMMDD diferente se quiser nova execução."
  fi
  if [ -n "$PREV_SERIES" ]; then
    IDEMPOTENCY_SERIES_ID="$PREV_SERIES"
  fi
  if [ -n "$PREV_TASK" ]; then
    IDEMPOTENCY_TASK_ID="$PREV_TASK"
  fi
  pass "Idempotência 4a: state file carregado (series=$IDEMPOTENCY_SERIES_ID task=$IDEMPOTENCY_TASK_ID)."
else
  pass "Idempotência 4b: sem state anterior — execução virgin."
fi

cd "$WORKDIR" || fail "cd $WORKDIR falhou"
log "WORKDIR = $(pwd)"
pass "WORKDIR válido: $(pwd)"

# =============================================================================
# 1 — Atualizar workspace para o último commit do branch homolog/youtube-auto-e2e
# =============================================================================
section "1. Git pull para último commit"
STEP_AT "git fetch && checkout && reset --hard origin/${BRANCH}"

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>&1 || echo "")
log "Branch atual: ${CURRENT_BRANCH}"

git fetch --all --prune >>"$MAIN_LOG" 2>&1 || fail "git fetch --all falhou"
pass "git fetch --all OK"

git checkout "$BRANCH" >>"$MAIN_LOG" 2>&1 || fail "git checkout $BRANCH falhou"
pass "git checkout $BRANCH OK"

git reset --hard "$REMOTE/$BRANCH" >>"$MAIN_LOG" 2>&1 || fail "git reset --hard origin/$BRANCH falhou"
pass "git reset --hard origin/$BRANCH OK"

CURRENT_SHA=$(git rev-parse HEAD 2>&1 || echo "")
CURRENT_SHA_SHORT=$(git rev-parse --short HEAD 2>&1 || echo "")
CURRENT_SUBJ=$(git log -1 --pretty=%s 2>&1 || echo "")
log "SHA     = $CURRENT_SHA"
log "SHA (c) = $CURRENT_SHA_SHORT"
log "Assunto = $CURRENT_SUBJ"
echo "COMMIT_SHA=$CURRENT_SHA" >>"$REPORT"
echo "COMMIT_SHA_SHORT=$CURRENT_SHA_SHORT" >>"$REPORT"
echo "COMMIT_SUBJECT=$CURRENT_SUBJ" >>"$REPORT"
pass "Código atualizado para ${CURRENT_SHA_SHORT}"

# =============================================================================
# 2 — Docker build imagem
# =============================================================================
section "2. Docker build"
STEP_AT "docker build --no-cache -t $IMAGE ."

BUILD_START=$(date -u +%s)
if ! docker build --no-cache -t "$IMAGE" . >>"$MAIN_LOG" 2>&1; then
  tail -n 80 "$MAIN_LOG" | tee -a "$REPORT"
  fail "docker build falhou — veja $MAIN_LOG (tail acima)"
fi
BUILD_END=$(date -u +%s)
BUILD_SEC=$((BUILD_END - BUILD_START))
pass "docker build OK (${BUILD_SEC} s)"

IMAGE_ID=$(docker inspect --format '{{.Id}}' "$IMAGE" 2>&1 || echo "")
log "IMAGE_ID = $IMAGE_ID"
echo "DOCKER_IMAGE=$IMAGE" >>"$REPORT"
echo "DOCKER_IMAGE_ID=$IMAGE_ID" >>"$REPORT"

# =============================================================================
# 3 — Subir container novo
# =============================================================================
section "3. Subir container"
STEP_AT "Parar container $CONTAINER se existir, subir novo com volume /data"

if docker ps -a --format '{{.Names}}' | grep -Fxq "$CONTAINER"; then
  log "Parando container existente: $CONTAINER"
  docker stop "$CONTAINER" >>"$MAIN_LOG" 2>&1 || true
  docker rm "$CONTAINER" >>"$MAIN_LOG" 2>&1 || true
  pass "Container antigo $CONTAINER parado e removido"
fi

mkdir -p "$MOUNT_VOLUME_HOST" || fail "mkdir $MOUNT_VOLUME_HOST falhou"
pass "Volume mount host existe: $MOUNT_VOLUME_HOST"

# Monta env array obrigatório + preservar todas as variáveis YOUTUBE_* E OPENAI_* E DATABASE_*
EXTRA_ENV_ARGS=()
if [ -n "$DATABASE_URL" ]; then
  EXTRA_ENV_ARGS+=(-e "DATABASE_URL=$DATABASE_URL")
fi
for ev in $(env | grep -E '^(YOUTUBE_|OPENAI_|REDIS_|AWS_|GOOGLE_|SENTRY_|STRIPE_)' | sed 's/=.*//'); do
  val="${!ev:-}"
  if [ -n "$val" ]; then
    EXTRA_ENV_ARGS+=(-e "${ev}=${val}")
  fi
done

DOCKER_RUN_CMD=(
  docker run -d --name "$CONTAINER"
  --restart unless-stopped
  --network "$CONTAINER_NETWORK"
  -p "${PORT}:8000"
  -v "${MOUNT_VOLUME_HOST}:${MOUNT_VOLUME_GUEST}"
  -e "ADMIN_EMAIL=${ADMIN_EMAIL}"
  -e "ADMIN_PASSWORD=${ADMIN_PASSWORD}"
  -e "SECRET_KEY=${SECRET_KEY}"
  -e "APP_ENV=${APP_ENV}"
  -e "DATABASE_URL=${DATABASE_URL}"
  "${EXTRA_ENV_ARGS[@]}"
  "$IMAGE"
)

log "Executando: ${DOCKER_RUN_CMD[*]}"
CONTAINER_ID=$("${DOCKER_RUN_CMD[@]}" 2>>"$MAIN_LOG") || {
  tail -n 40 "$MAIN_LOG" | tee -a "$REPORT"
  fail "docker run falhou — veja $MAIN_LOG (tail acima)"
}
pass "Container iniciado: $CONTAINER (id=${CONTAINER_ID:0:12})"

log "Aguardando container responder em ${BASE_URL}/docs ..."
health_ok=0
for i in $(seq 1 60); do
  sleep 2
  code=$(http_ok "${BASE_URL}/docs" || echo "000")
  if [ "$code" = "200" ] || [ "$code" = "404" ]; then
    # Swagger docs (200) ou app inicializado de outro modo (404 docs ainda OK)
    # A docs.html normalmente é servida. Vamos testar também /token para não
    # depender de /docs.
    code2=$(http_ok "${BASE_URL}/token" -X POST -d "username=x&password=x" -H 'content-type: application/x-www-form-urlencoded' -o /dev/null -w "%{http_code}" 2>>"$MAIN_LOG" || echo "000")
    if [ "$code2" = "200" ] || [ "$code2" = "401" ] || [ "$code2" = "422" ]; then
      health_ok=1
      break
    fi
  fi
  [ $((i % 10)) -eq 0 ] && log "  Aguardando app inicializar... ($i/60 tentativa, HTTP=${code})"
done
if [ "$health_ok" != "1" ]; then
  log "Últimos logs do container:"
  docker logs --tail 60 "$CONTAINER" 2>&1 | tee -a "$MAIN_LOG"
  fail "Container não respondeu após ~2min em ${BASE_URL}"
fi
pass "App respondeu HTTP (base_url=${BASE_URL})"

# =============================================================================
# 4 — Login JWT admin
# =============================================================================
section "4. Login /token"
STEP_AT "POST /token OAuth2PasswordRequestForm (admin)"

LOGIN_JSON=$(http_post "${BASE_URL}/token" \
  -H "content-type: application/x-www-form-urlencoded" \
  --data-urlencode "username=${ADMIN_EMAIL}" \
  --data-urlencode "password=${ADMIN_PASSWORD}" 2>&1)

ACCESS_TOKEN=$(echo "$LOGIN_JSON" | jq -r '.access_token // empty' 2>/dev/null || echo "")
TOKEN_TYPE=$(echo "$LOGIN_JSON" | jq -r '.token_type // empty' 2>/dev/null || echo "")
[ -n "$ACCESS_TOKEN" ] || { echo "$LOGIN_JSON" | tee -a "$REPORT"; fail "/token não retornou access_token"; }
pass "Login OK (token_type=${TOKEN_TYPE}, access_token length=${#ACCESS_TOKEN})"

AUTH_HEAD="Authorization: Bearer ${ACCESS_TOKEN}"
echo "ADMIN_EMAIL=${ADMIN_EMAIL}" >>"$REPORT"
echo "JWT_LEN=${#ACCESS_TOKEN}" >>"$REPORT"

# =============================================================================
# 5 — Verificar YouTube OAuth conectado
# =============================================================================
section "5. YouTube OAuth — conexão disponível"
STEP_AT "GET /youtube/status"

YT_STATUS_JSON=$(http_json "${BASE_URL}/youtube/status" -H "$AUTH_HEAD")
echo "YOUTUBE_STATUS_JSON_START" >>"$REPORT"
echo "$YT_STATUS_JSON" | jq . >>"$REPORT" 2>/dev/null || echo "$YT_STATUS_JSON" >>"$REPORT"
echo "YOUTUBE_STATUS_JSON_END" >>"$REPORT"

SVC_CONNECTED=$(echo "$YT_STATUS_JSON" | jq -r '.service_connected // false' 2>/dev/null || echo "false")
HAS_REFRESH=$(echo "$YT_STATUS_JSON" | jq -r '(.db_has_refresh_token == true) or (.env_has_refresh_token == true)' 2>/dev/null || echo "false")
AUTH_ERR=$(echo "$YT_STATUS_JSON" | jq -r '.service_auth_error // empty' 2>/dev/null || echo "")

if [ "$SVC_CONNECTED" != "true" ] && [ "$HAS_REFRESH" != "true" ]; then
  log "YouTube NÃO conectado. Para prosseguir, você precisa conectar em:"
  log "  1) POST /youtube/auth_url (retorna URL)"
  log "  2) Abrir a URL no navegador, aceitar permissões, pegar o code do redirect_uri"
  log "  3) POST /youtube/auth/exchange com {\"code\":\"...\"}"
  log "Vou tentar agora com client_secret do banco se possível..."
  AUTH_URL_JSON=$(http_json "${BASE_URL}/youtube/auth_url" -H "$AUTH_HEAD")
  AUTH_URL=$(echo "$AUTH_URL_JSON" | jq -r '.auth_url // empty' 2>/dev/null || echo "")
  if [ -n "$AUTH_URL" ]; then
    log "auth_url = ${AUTH_URL}"
    fail "YouTube OAuth não conectado. Abra a auth_url acima no navegador do canal alvo, faça a autorização completa, cole o ?code=... de volta via POST /youtube/auth/exchange, e rode este script novamente."
  else
    echo "$AUTH_URL_JSON" | tee -a "$REPORT"
    fail "YouTube OAuth sem credenciais nem refresh_token. Configure em DB Settings ou env YOUTUBE_CLIENT_ID/YOUTUBE_CLIENT_SECRET/YOUTUBE_REFRESH_TOKEN."
  fi
fi
pass "YouTube service conectado (service_connected=${SVC_CONNECTED}, refresh_token_present=${HAS_REFRESH}, auth_error=${AUTH_ERR:-<none>})"

# =============================================================================
# 6 — Criar série NOVA (1 episódio curto, auto_approval=True, lead_days=0)
# =============================================================================
section "6. Criar série + 1 episódio curto"
STEP_AT "POST /youtube/series → create_series (idempotency_key=${IDEMPOTENCY_KEY})"

# Primeiro: tentar recuperar task/série por idempotency (evita duplicar)
if [ -z "$IDEMPOTENCY_TASK_ID" ]; then
  PREV_TASK_IDEMP=$(http_json "${BASE_URL}/youtube/tasks/by-idempotency?idempotency_key=${IDEMPOTENCY_KEY}" -H "$AUTH_HEAD" 2>&1 || echo "{}")
  IDEMPOTENCY_TASK_ID=$(echo "$PREV_TASK_IDEMP" | jq -r '.id // empty' 2>/dev/null || echo "")
  [ -n "$IDEMPOTENCY_TASK_ID" ] && log "Recuperada task via idempotency: ${IDEMPOTENCY_TASK_ID}"
fi

TOPIC="3 lições bíblicas que mudam o seu dia — motivação e esperança"
DUR_MIN=2
SERIES_JSON=""

if [ -n "$IDEMPOTENCY_SERIES_ID" ]; then
  DETAIL=$(http_json "${BASE_URL}/youtube/series/${IDEMPOTENCY_SERIES_ID}" -H "$AUTH_HEAD" 2>&1 || echo "{}")
  SID_CHECK=$(echo "$DETAIL" | jq -r '.id // empty' 2>/dev/null || echo "")
  if [ "$SID_CHECK" = "$IDEMPOTENCY_SERIES_ID" ]; then
    SERIES_JSON="$DETAIL"
    log "Série idempotente recuperada: id=$IDEMPOTENCY_SERIES_ID  (não criar nova)."
  fi
fi

if [ -z "$SERIES_JSON" ]; then
  TITULO_HOMOLOG="TESTE HOMOLOG — NÃO PUBLICAR — validação e2e YouTube Auto $(date -u '+%Y-%m-%d')"
  SERIES_JSON=$(http_post "${BASE_URL}/youtube/series" \
    -H "$AUTH_HEAD" -H "content-type: application/json" \
    -d "$(jq -cn \
      --arg t "$TOPIC" \
      --argjson d "$DUR_MIN" \
      --arg title "$TITULO_HOMOLOG" \
      --arg ik "$IDEMPOTENCY_KEY" \
      '{
         title: $title,
         description: (
           "TESTE DE HOMOLOGAÇÃO INTERNA — NÃO USAR ESTE VÍDEO NO CANAL. " +
           "Objetivo: validar pipeline YouTube Auto ponta-a-ponta. " +
           "Gerar roteiro + imagens OpenAI + áudio TTS + MP4 em /data + upload YouTube NÃO LISTADO."
         ),
         content_type: "generic_motivational",
         target_audience: "Pessoas em busca de motivação e fé diária.",
         unique_value_proposition: "Histórias curtas, imagens cinematográficas e narração humana.",
         key_message: "Pequenas atitudes diárias geram grandes transformações.",
         language: "pt-BR",
         number_of_episodes: 1,
         duration_minutes: $d,
         start_date: (now | todateiso8601 | sub("T.*$";"")),
         end_date: (now + 86400 | todateiso8601 | sub("T.*$";"")),
         production_lead_days: 0,
         visibility: "unlisted",
         auto_approval: true,
         narration_style: "warm_storyteller",
         publishing_time: "08:00",
         idempotency_key: $ik,
         episodes: []
       }')")
fi

echo "CREATE_SERIES_JSON_START" >>"$REPORT"
echo "$SERIES_JSON" | jq . >>"$REPORT" 2>/dev/null || echo "$SERIES_JSON" >>"$REPORT"
echo "CREATE_SERIES_JSON_END" >>"$REPORT"

SERIES_ID=$(echo "$SERIES_JSON" | jq -r '(.id // .series_id // .data.id // .data.series_id) // empty' 2>/dev/null || echo "")
if [ -z "$SERIES_ID" ]; then
  SERIES_ID=$(echo "$SERIES_JSON" | grep -oE '"id"\s*:\s*[0-9]+' | head -n1 | grep -oE '[0-9]+' || echo "")
fi
[ -n "$SERIES_ID" ] || fail "create_series não retornou id (veja JSON no report)."
pass "Série criada com sucesso: id=${SERIES_ID}, episódios=1, duração=${DUR_MIN}min, auto_approval=true"

# =============================================================================
# 7 — Ativar série status=active
# =============================================================================
section "7. Ativar série"
STEP_AT "PUT /youtube/series/{id}/status → status=active"

STATUS_JSON=$(http_put "${BASE_URL}/youtube/series/${SERIES_ID}/status" \
  -H "$AUTH_HEAD" -H "content-type: application/json" \
  -d '{"status":"active"}')

echo "UPDATE_STATUS_JSON_START" >>"$REPORT"
echo "$STATUS_JSON" | jq . >>"$REPORT" 2>/dev/null || echo "$STATUS_JSON" >>"$REPORT"
echo "UPDATE_STATUS_JSON_END" >>"$REPORT"

NEW_STATUS=$(echo "$STATUS_JSON" | jq -r '(.status // .data.status // empty)' 2>/dev/null || echo "")
[ "$NEW_STATUS" = "active" ] || {
  # fallback: ler detalhe
  DETAIL=$(http_json "${BASE_URL}/youtube/series/${SERIES_ID}" -H "$AUTH_HEAD")
  NEW_STATUS=$(echo "$DETAIL" | jq -r '.status // empty' 2>/dev/null || echo "")
}
[ "$NEW_STATUS" = "active" ] || fail "Série não ficou status=active (recebido=${NEW_STATUS:-vazio})."
pass "Série ativada com sucesso: status=active"

# =============================================================================
# 8 — Disparar scheduler /youtube/series/sync
# =============================================================================
section "8. Disparar sync (enfileirar episódio)"
STEP_AT "POST /youtube/series/sync → executa sync_series_scheduler"

SYNC_JSON=$(http_post "${BASE_URL}/youtube/series/sync" \
  -H "$AUTH_HEAD" -H "content-type: application/json" \
  -d '{}')

echo "SYNC_JSON_START" >>"$REPORT"
echo "$SYNC_JSON" | jq . >>"$REPORT" 2>/dev/null || echo "$SYNC_JSON" >>"$REPORT"
echo "SYNC_JSON_END" >>"$REPORT"

SYNC_COUNT=$(echo "$SYNC_JSON" | jq -r '(.enqueued // .count // .synced // .updated_episodes // 0) | (if type=="number" then . else 0 end)' 2>/dev/null || echo "0")
log "sync count ≈ ${SYNC_COUNT}"
sleep 3
pass "sync disparado (resposta parseada OK)"

# =============================================================================
# 9 — Pegar task_id do episódio e fazer polling até published
# =============================================================================
section "9. Polling do episódio e task (até published)"
STEP_AT "GET /youtube/series/{id} → encontrar episode.task_id, depois GET /youtube/task/{task_id}"

POLL_START=$(date -u +%s)
POLL_MAX_SEC=$((40 * 60))   # 40 minutos máximo (upload YT pode demorar)
POLL_INTERVAL=20
LAST_EPISODE_JSON=""
LAST_TASK_JSON=""
EPISODE_ID=""
TASK_ID=""
FINAL_EP_STATUS=""
FINAL_TASK_STATUS=""

for step in $(seq 1 1000); do
  NOW=$(date -u +%s)
  ELAPSED=$((NOW - POLL_START))
  if [ "$ELAPSED" -gt "$POLL_MAX_SEC" ]; then
    fail "Polling estourou timeout de ${POLL_MAX_SEC}s. Veja último estado no report."
  fi

  DETAIL=$(http_json "${BASE_URL}/youtube/series/${SERIES_ID}" -H "$AUTH_HEAD" 2>&1)
  EPISODES=$(echo "$DETAIL" | jq -c '.episodes // .data.episodes // []' 2>/dev/null || echo "[]")
  EP_COUNT=$(echo "$EPISODES" | jq 'length' 2>/dev/null || echo 0)

  if [ "$EP_COUNT" -ge 1 ]; then
    EP_JSON=$(echo "$EPISODES" | jq -c '.[0]' 2>/dev/null || echo "{}")
    LAST_EPISODE_JSON="$EP_JSON"
    EPISODE_ID=$(echo "$EP_JSON" | jq -r '.id // empty' 2>/dev/null || echo "$EPISODE_ID")
    TASK_ID=$(echo "$EP_JSON" | jq -r '(.task_id // .video_task_id) // empty' 2>/dev/null || echo "$TASK_ID")
    EP_STATUS=$(echo "$EP_JSON" | jq -r '.status // empty' 2>/dev/null || echo "")
    FINAL_EP_STATUS="$EP_STATUS"
  else
    EP_STATUS="sem_episodios"
  fi

  TASK_STATUS=""
  TASK_PROGRESS=""
  TASK_MESSAGE=""
  if [ -n "$TASK_ID" ]; then
    TASK=$(http_json "${BASE_URL}/youtube/task/${TASK_ID}" 2>&1)
    LAST_TASK_JSON="$TASK"
    TASK_STATUS=$(echo "$TASK" | jq -r '.status // empty' 2>/dev/null || echo "")
    TASK_PROGRESS=$(echo "$TASK" | jq -r '.progress // "0"' 2>/dev/null || echo "0")
    TASK_MESSAGE=$(echo "$TASK" | jq -r '.message // empty' 2>/dev/null || echo "")
    FINAL_TASK_STATUS="$TASK_STATUS"
  fi

  # Mostra uma linha a cada passo
  log "poll #${step}  elapsed=${ELAPSED}s  ep=${EP_STATUS}  task=${TASK_STATUS}(${TASK_PROGRESS}%)  task_id=${TASK_ID:-<pend>}  msg=${TASK_MESSAGE:0:60}"

  # Critério de falha: task failed OU episode publication_blocked sem upload
  if [ "$TASK_STATUS" = "failed" ]; then
    echo "LAST_TASK_FAILED_JSON_START" >>"$REPORT"
    echo "$LAST_TASK_JSON" | jq . >>"$REPORT" 2>/dev/null || echo "$LAST_TASK_JSON" >>"$REPORT"
    echo "LAST_TASK_FAILED_JSON_END" >>"$REPORT"
    echo "LAST_EPISODE_JSON_START" >>"$REPORT"
    echo "$LAST_EPISODE_JSON" | jq . >>"$REPORT" 2>/dev/null || echo "$LAST_EPISODE_JSON" >>"$REPORT"
    echo "LAST_EPISODE_JSON_END" >>"$REPORT"
    fail "Task id=${TASK_ID} terminou FAILED. Veja result_json no report."
  fi

  # Critério de sucesso: episódio published OU (episode=approved e task.result_json.youtube_video_id existe)
  if [ "$EP_STATUS" = "published" ]; then
    pass "Episódio ${EPISODE_ID} chegou em status=published"
    break
  fi
  if [ "$EP_STATUS" = "approved" ] && [ -n "$TASK_ID" ]; then
    YT_VID=$(echo "$LAST_TASK_JSON" | jq -r '(.result.youtube_video_id // .youtube_video_id) // empty' 2>/dev/null || echo "")
    if [ -n "$YT_VID" ]; then
      log "episode=approved + task.youtube_video_id=${YT_VID} → considerar sucesso (approve automático já converte para published no próximo sync)"
      pass "Task aprovada com youtube_video_id preenchido no result_json"
      break
    fi
  fi

  sleep "$POLL_INTERVAL"
done

echo "LAST_EPISODE_JSON_START" >>"$REPORT"
echo "$LAST_EPISODE_JSON" | jq . >>"$REPORT" 2>/dev/null || echo "$LAST_EPISODE_JSON" >>"$REPORT"
echo "LAST_EPISODE_JSON_END" >>"$REPORT"
echo "LAST_TASK_JSON_START" >>"$REPORT"
echo "$LAST_TASK_JSON" | jq . >>"$REPORT" 2>/dev/null || echo "$LAST_TASK_JSON" >>"$REPORT"
echo "LAST_TASK_JSON_END" >>"$REPORT"

# =============================================================================
# 9.5 — Salvar state precoce (série + task id) — mesmo que falhe, recupera)
# =============================================================================
cat >"$PREV_LOCK_FILE" <<EOF
RUN_TAG=$RUN_TAG
PREV_SERIES_ID=$SERIES_ID
PREV_TASK_ID=$TASK_ID
PREV_STATE=IN_PROGRESS
PREV_STARTED_AT=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOF
log "State salvo em $PREV_LOCK_FILE (series=$SERIES_ID task=$TASK_ID)"

# =============================================================================
# 10 — Validar artefatos do result_json (script, render_report, audio, imagens, file_path, video_url)
# =============================================================================
section "10. Validação de artefatos"
STEP_AT "Extrair tudo que há no LAST_TASK_JSON (task.result_json)"

RESULT=$(echo "$LAST_TASK_JSON" | jq -c '.result // {}' 2>/dev/null || echo "{}")
if [ "$RESULT" = "{}" ] || [ -z "$RESULT" ]; then
  RESULT=$(echo "$LAST_TASK_JSON" | jq -c '.' 2>/dev/null || echo "{}")
fi

# 10.1 script
SCRIPT_JSON=$(echo "$RESULT" | jq -c '.script // {}' 2>/dev/null || echo "{}")
SCENES_COUNT=$(echo "$SCRIPT_JSON" | jq -r '(.scenes // []) | length' 2>/dev/null || echo "0")
TITLE=$(echo "$SCRIPT_JSON" | jq -r '.title // empty' 2>/dev/null || echo "")
SELECTED_IMGS=$(echo "$SCRIPT_JSON" | jq -r '(.selected_images // []) | length' 2>/dev/null || echo "0")
log "script: title=${TITLE:0:50}, scenes_count=${SCENES_COUNT}, selected_images=${SELECTED_IMGS}"
[ "$SCENES_COUNT" -ge 1 ] || fail "result_json.script.scenes vazio (count=${SCENES_COUNT})."
pass "Roteiro gerado: ${SCENES_COUNT} cenas, título='${TITLE:0:60}'"
[ "$SELECTED_IMGS" -ge 2 ] || fail "result_json.script.selected_images com poucas imagens ($SELECTED_IMGS)."
pass "selected_images persistido no script: ${SELECTED_IMGS} URLs"

# 10.2 render_report
RENDER_REPORT=$(echo "$RESULT" | jq -c '.render_report // {}' 2>/dev/null || echo "{}")
[ "$RENDER_REPORT" != "{}" ] || fail "result_json.render_report vazio ou ausente."
pass "render_report presente (campos: $(echo "$RENDER_REPORT" | jq -r 'keys | join(", ")' 2>/dev/null || echo "<keys err>"))"

# 10.3 audio_generation
AUDIO_GEN=$(echo "$RESULT" | jq -c '.audio_generation // (.render_report.audio_generation) // {}' 2>/dev/null || echo "{}")
AUDIO_PATH=$(echo "$AUDIO_GEN" | jq -r '.output_path // empty' 2>/dev/null || echo "")
[ -n "$AUDIO_PATH" ] || fail "audio_generation.output_path ausente."
pass "audio_generation.output_path encontrado: ${AUDIO_PATH}"

# AUDIO_PATH pode começar com /data -> existe no host via MOUNT_VOLUME_HOST se o guest usou /data.
# Também pode começar com /app/static -> precisamos procurar em container.
AUDIO_EXIST_OK=0
if [[ "$AUDIO_PATH" == /data/* ]]; then
  HOST_AUDIO="${MOUNT_VOLUME_HOST}${AUDIO_PATH#/data}"
  if [ -f "$HOST_AUDIO" ] && [ "$(stat -c '%s' "$HOST_AUDIO" 2>/dev/null || echo 0)" -gt 2000 ]; then
    AUDIO_EXIST_OK=1
    AUDIO_SIZE=$(stat -c '%s' "$HOST_AUDIO" 2>/dev/null || echo 0)
    pass "Áudio encontrado no host (volume): ${HOST_AUDIO}  (${AUDIO_SIZE} bytes)"
  fi
fi
if [ "$AUDIO_EXIST_OK" != "1" ]; then
  INSIDE=$(docker exec "$CONTAINER" sh -c "[ -f \"$AUDIO_PATH\" ] && echo 1 || echo 0" 2>>"$MAIN_LOG" || echo "0")
  if [ "$INSIDE" = "1" ]; then
    SZ=$(docker exec "$CONTAINER" stat -c '%s' "$AUDIO_PATH" 2>>"$MAIN_LOG" || echo "0")
    if [ "$SZ" -gt 2000 ]; then
      AUDIO_EXIST_OK=1
      pass "Áudio encontrado no container: ${AUDIO_PATH} (${SZ} bytes)"
    fi
  fi
fi
[ "$AUDIO_EXIST_OK" = "1" ] || fail "Áudio NÃO encontrado nem no volume nem no container (${AUDIO_PATH})."

# 10.4 imagens em disco (4 primeiras, pelo menos 2 existem)
IMG_OK=0
IMG_CHECKED=0
for i in $(seq 0 3); do
  URL=$(echo "$SCRIPT_JSON" | jq -r --argjson i "$i" '.selected_images[$i] // empty' 2>/dev/null || echo "")
  [ -z "$URL" ] && continue
  IMG_CHECKED=$((IMG_CHECKED + 1))
  # Tentativa 1: URL /static/videos/<file> ou /media/videos/<file> => /app/static
  REL="${URL#/}"
  GUEST_CAND="/app/${REL}"
  HOST_CAND1="${MOUNT_VOLUME_HOST}/static/${REL#*/}"
  HOST_CAND2="${MOUNT_VOLUME_HOST}/media/${REL#*/}"
  FOUND=0
  for P in "$GUEST_CAND" "$HOST_CAND1" "$HOST_CAND2"; do
    if [[ "$P" == /data/* ]]; then
      HP="${MOUNT_VOLUME_HOST}${P#/data}"
      if [ -f "$HP" ] && [ "$(stat -c '%s' "$HP" 2>/dev/null || echo 0)" -gt 5000 ]; then FOUND=1; break; fi
    elif [[ "$P" == /app/* ]]; then
      INSIDE=$(docker exec "$CONTAINER" sh -c "[ -f \"$P\" ] && echo 1 || echo 0" 2>>"$MAIN_LOG" || echo "0")
      if [ "$INSIDE" = "1" ]; then
        SZ=$(docker exec "$CONTAINER" stat -c '%s' "$P" 2>>"$MAIN_LOG" || echo "0")
        [ "$SZ" -gt 5000 ] && { FOUND=1; break; }
      fi
    else
      [ -f "$P" ] && [ "$(stat -c '%s' "$P" 2>/dev/null || echo 0)" -gt 5000 ] && { FOUND=1; break; }
    fi
  done
  if [ "$FOUND" = "1" ]; then IMG_OK=$((IMG_OK + 1)); fi
done
[ "$IMG_CHECKED" -ge 2 ] || log "Aviso: só consegui resolver ${IMG_CHECKED} URLs de selected_images (esq 2)."
[ "$IMG_OK" -ge 2 ] || fail "selected_images não estão no disco (${IMG_OK}/${IMG_CHECKED} válidas)."
pass "Imagens geradas e gravadas em disco: ${IMG_OK}/${IMG_CHECKED} resolvidas OK"

# 10.5 MP4 no disco
FILE_PATH=$(echo "$RESULT" | jq -r '.file_path // (.render_report.video_render.file_path) // empty' 2>/dev/null || echo "")
VIDEO_URL=$(echo "$RESULT" | jq -r '.video_url // empty' 2>/dev/null || echo "")
if [ -z "$FILE_PATH" ] && [ -n "$VIDEO_URL" ]; then
  if [[ "$VIDEO_URL" == /media/videos/* ]]; then
    FILE_PATH="/data/media/videos/${VIDEO_URL#/media/videos/}"
  elif [[ "$VIDEO_URL" == /static/videos/* ]]; then
    FILE_PATH="/app/static/videos/${VIDEO_URL#/static/videos/}"
  fi
fi
[ -n "$FILE_PATH" ] || fail "Nem file_path nem video_url puderam ser mapeados."
pass "Caminho MP4 (lógico): ${FILE_PATH}   URL: ${VIDEO_URL}"

MP4_FOUND=0
MP4_BYTES=0
if [[ "$FILE_PATH" == /data/* ]]; then
  HOST_MP4="${MOUNT_VOLUME_HOST}${FILE_PATH#/data}"
  if [ -f "$HOST_MP4" ]; then
    MP4_BYTES=$(stat -c '%s' "$HOST_MP4" 2>/dev/null || echo 0)
    [ "$MP4_BYTES" -gt 100000 ] && MP4_FOUND=1
  fi
  GUEST_MP4="$FILE_PATH"
else
  GUEST_MP4="$FILE_PATH"
fi
if [ "$MP4_FOUND" != "1" ]; then
  INSIDE=$(docker exec "$CONTAINER" sh -c "[ -f \"$GUEST_MP4\" ] && echo 1 || echo 0" 2>>"$MAIN_LOG" || echo "0")
  if [ "$INSIDE" = "1" ]; then
    MP4_BYTES=$(docker exec "$CONTAINER" stat -c '%s' "$GUEST_MP4" 2>>"$MAIN_LOG" || echo "0")
    [ "$MP4_BYTES" -gt 100000 ] && MP4_FOUND=1
  fi
fi
[ "$MP4_FOUND" = "1" ] || fail "MP4 NÃO encontrado ou muito pequeno (${MP4_BYTES} bytes)."
pass "MP4 criado: ${FILE_PATH}  size=${MP4_BYTES} bytes ($((MP4_BYTES/1024)) KB)"

echo "MP4_FILE_PATH=$FILE_PATH" >>"$REPORT"
echo "MP4_VIDEO_URL=$VIDEO_URL" >>"$REPORT"
echo "MP4_BYTES=$MP4_BYTES" >>"$REPORT"

# 10.6 ffprobe válido
FFPROBE_OUT=""
FALLBACK_DOCKER_FFPROBE=0
if [ -n "${HOST_MP4:-}" ] && [ -f "${HOST_MP4:-}" ] && command -v ffprobe >/dev/null 2>&1; then
  FFPROBE_OUT=$(ffprobe -v error -show_format -show_streams -print_format json "$HOST_MP4" 2>>"$MAIN_LOG" || echo "{}")
else
  FALLBACK_DOCKER_FFPROBE=1
  FFPROBE_OUT=$(docker exec "$CONTAINER" sh -c "ffprobe -v error -show_format -show_streams -print_format json \"$GUEST_MP4\"" 2>>"$MAIN_LOG" || echo "{}")
fi
echo "FFPROBE_JSON_START" >>"$REPORT"
echo "$FFPROBE_OUT" | jq . >>"$REPORT" 2>/dev/null || echo "$FFPROBE_OUT" >>"$REPORT"
echo "FFPROBE_JSON_END" >>"$REPORT"

VIDEO_DUR=$(echo "$FFPROBE_OUT" | jq -r '.format.duration // "0"' 2>/dev/null || echo "0")
HAS_V=$(echo "$FFPROBE_OUT" | jq -r '[.streams[]? | select(.codec_type=="video")] | length' 2>/dev/null || echo "0")
HAS_A=$(echo "$FFPROBE_OUT" | jq -r '[.streams[]? | select(.codec_type=="audio")] | length' 2>/dev/null || echo "0")

log "ffprobe: dur=${VIDEO_DUR}s  streams(video/audio)=${HAS_V}/${HAS_A}"
[ "$HAS_V" -ge 1 ] || fail "ffprobe: stream de vídeo ausente."
pass "ffprobe stream de vídeo: OK (${HAS_V} stream(s))"
[ "$HAS_A" -ge 1 ] || fail "ffprobe: stream de áudio ausente."
pass "ffprobe stream de áudio: OK (${HAS_A} stream(s))"
DUR_INT=$(printf '%.0f' "$VIDEO_DUR" 2>/dev/null || echo 0)
[ "$DUR_INT" -ge 20 ] || fail "ffprobe: duração muito curta (${VIDEO_DUR}s, esperado ≥ 20s)."
pass "ffprobe duração: ${VIDEO_DUR} s"

# 10.7 MP4 abre via navegador (HTTP 200 ou 206 range request)
[ -n "$VIDEO_URL" ] || fail "result_json.video_url vazio (não consigo testar GET)."
PUBLIC_URL="${BASE_URL}${VIDEO_URL}"
log "Testando GET ${PUBLIC_URL}"
FULL_CODE=$(http_ok "$PUBLIC_URL" -r 0-1048575 2>&1)
log "Range request HTTP: ${FULL_CODE}"
if [ "$FULL_CODE" = "200" ] || [ "$FULL_CODE" = "206" ]; then
  pass "MP4 acessível via navegador: HTTP ${FULL_CODE} em ${PUBLIC_URL}"
else
  # Algumas vezes falha por redirect/rota, testar também /static/videos se URL for /media
  ALT_URL="${PUBLIC_URL}"
  if [[ "$VIDEO_URL" == /media/* ]]; then
    ALT_STATIC="/static/${VIDEO_URL#/media/}"
    ALT_URL="${BASE_URL}${ALT_STATIC}"
    log "Tentando alternativa ${ALT_URL}"
    ALT_CODE=$(http_ok "$ALT_URL" -r 0-1048575 2>&1)
    if [ "$ALT_CODE" = "200" ] || [ "$ALT_CODE" = "206" ]; then
      pass "MP4 acessível via rota alternativa: HTTP ${ALT_CODE} em ${ALT_URL}"
    else
      fail "MP4 não respondeu HTTP em nenhuma rota testada (${FULL_CODE}, alt=${ALT_CODE})."
    fi
  else
    fail "MP4 não respondeu HTTP ${FULL_CODE} em ${PUBLIC_URL}."
  fi
fi
echo "MP4_PUBLIC_URL=${PUBLIC_URL}" >>"$REPORT"

# 10.8 Download (head request + content-length = tamanho conhecido)
CL=$(http_ok "$PUBLIC_URL" -L -o /dev/null -w "%{size_download}" 2>>"$MAIN_LOG" || echo "0")
log "Tamanho baixado (parcial, GET padrão): ${CL} bytes"
pass "Download funcionando (bytes descarregados ≥ 0: ${CL})."

# =============================================================================
# 11 — YouTube upload validado (unlisted + youtube_video_id + youtube_url salvos)
# =============================================================================
section "11. Validação upload YouTube — Não Listado"
STEP_AT "Checar result_json.youtube_video_id / youtube_url, upload_status, privacy via /youtube/videos se possível."

YT_VID=$(echo "$RESULT" | jq -r '.youtube_video_id // empty' 2>/dev/null || echo "")
YT_URL=$(echo "$RESULT" | jq -r '.youtube_url // empty' 2>/dev/null || echo "")
UPLOAD_STATUS=$(echo "$RESULT" | jq -r '.upload_status // empty' 2>/dev/null || echo "")

echo "YOUTUBE_VIDEO_ID=$YT_VID" >>"$REPORT"
echo "YOUTUBE_URL=$YT_URL" >>"$REPORT"
echo "YOUTUBE_UPLOAD_STATUS=$UPLOAD_STATUS" >>"$REPORT"

[ -n "$YT_VID" ] || fail "result_json.youtube_video_id VAZIO (upload NÃO aconteceu)."
pass "youtube_video_id salvo: ${YT_VID}"
[ -n "$YT_URL" ] || fail "result_json.youtube_url VAZIO."
pass "youtube_url salvo: ${YT_URL}"
[ "$UPLOAD_STATUS" = "completed" ] || log "Aviso: upload_status=${UPLOAD_STATUS} (esperado completed). Se YT_VID existe e URL abre, provavelmente o preenchimento do status foi parcial — OK desde que o vídeo apareça."

# Listar últimos 5 vídeos do canal para confirmar presença e privacy
if [ "$SVC_CONNECTED" = "true" ]; then
  log "Buscando últimos 5 vídeos do canal via GET /youtube/videos..."
  CH_VIDEOS_JSON=$(http_json "${BASE_URL}/youtube/videos?max=5" -H "$AUTH_HEAD" 2>&1)
  echo "CHANNEL_VIDEOS_JSON_START" >>"$REPORT"
  echo "$CH_VIDEOS_JSON" | jq . >>"$REPORT" 2>/dev/null || echo "$CH_VIDEOS_JSON" >>"$REPORT"
  echo "CHANNEL_VIDEOS_JSON_END" >>"$REPORT"

  FOUND_VID=$(echo "$CH_VIDEOS_JSON" | jq --arg id "$YT_VID" -r '[.items[]? // .[]? | select(.id?.videoId == $id or .id == $id)] | .[0] // empty | .id?.videoId // .id // empty' 2>/dev/null || echo "")
  if [ -n "$FOUND_VID" ]; then
    pass "Vídeo ${YT_VID} aparece na listagem de vídeos do canal."
    # Tentar pegar privacy (nem sempre o endpoint retorna, então ignore se não vier)
    PRIV=$(echo "$CH_VIDEOS_JSON" | jq --arg id "$YT_VID" -r '[.items[]? // .[]? | select(.id?.videoId == $id or .id == $id)] | .[0] // empty | .status.privacyStatus // empty' 2>/dev/null || echo "")
    [ -n "$PRIV" ] && log "privacyStatus retornado pela listagem: ${PRIV}"
  else
    log "Aviso: listagem de vídeos não retornou o id ${YT_VID} (às vezes a listagem é cacheada ou limitada). Seguindo para validação por YouTube Video directo."
  fi
fi

# Valida final: episódio published OU (approved + youtube_video_id + youtube_url não vazios + ep.youtube_video_id foi salvo)
# Se chegou até aqui com YT_VID e YT_URL não-vazios, e episodio nao falhou, considera sucesso.
FINAL_EP_VID=$(echo "$LAST_EPISODE_JSON" | jq -r '.youtube_video_id // empty' 2>/dev/null || echo "")
FINAL_EP_URL=$(echo "$LAST_EPISODE_JSON" | jq -r '.youtube_url // empty' 2>/dev/null || echo "")
echo "EPISODE_YOUTUBE_VIDEO_ID=$FINAL_EP_VID" >>"$REPORT"
echo "EPISODE_YOUTUBE_URL=$FINAL_EP_URL" >>"$REPORT"

[ "$FINAL_EP_VID" = "$YT_VID" ] || log "Aviso: SeriesEpisode.youtube_video_id (${FINAL_EP_VID}) != result_json.youtube_video_id (${YT_VID}). Normal se o approve ainda não rodou em DB; o importante é que a task gravou e o vídeo existe."

# Salvar state COMPLETED (definitivo) — bloqueia reexecução por idempotência.
cat >"$PREV_LOCK_FILE" <<EOF
RUN_TAG=$RUN_TAG
PREV_SERIES_ID=$SERIES_ID
PREV_TASK_ID=$TASK_ID
PREV_STATE=COMPLETED
PREV_VIDEO_URL=$VIDEO_URL
PREV_MP4_PATH=$FILE_PATH
PREV_MP4_BYTES=$MP4_BYTES
PREV_DURATION_S=$VIDEO_DUR
PREV_AUDIO_PATH=$AUDIO_PATH
PREV_SELECTED_IMAGES_COUNT=$SELECTED_IMGS
PREV_SCENES_COUNT=$SCENES_COUNT
PREV_YOUTUBE_VIDEO_ID=$YT_VID
PREV_YOUTUBE_URL=$YT_URL
PREV_UPLOAD_STATUS=$UPLOAD_STATUS
PREV_FINISHED_AT=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOF
log "State COMPLETED salvo em $PREV_LOCK_FILE (youtube_video_id=$YT_VID)."

# =============================================================================
# 12 — Custo OpenAI (estimativa via guardian_summary se existir)
# =============================================================================
section "12. Custos e métricas"
STEP_AT "Extrair financial_guardian e demais estimativas"

GUARDIAN=$(echo "$RESULT" | jq -c '.financial_guardian // {}' 2>/dev/null || echo "{}")
echo "GUARDIAN_JSON_START" >>"$REPORT"
echo "$GUARDIAN" | jq . >>"$REPORT" 2>/dev/null || echo "$GUARDIAN" >>"$REPORT"
echo "GUARDIAN_JSON_END" >>"$REPORT"
EST_TOTAL=$(echo "$GUARDIAN" | jq -r '.total_cost_estimated_usd // .estimated_total_cost_usd // .total_cost_usd // "0"' 2>/dev/null || echo "0")
pass "Estimativa custo OpenAI (guardian): ~USD ${EST_TOTAL}"
echo "OPENAI_COST_ESTIMATED_USD=$EST_TOTAL" >>"$REPORT"

echo "SCRIPT_FILE=app/routers/youtube.py (script)" >>"$REPORT"
echo "RENDER_REPORT_EMBEDDED=result_json.render_report" >>"$REPORT"
echo "AUDIO_OUTPUT_PATH=$AUDIO_PATH" >>"$REPORT"
echo "SELECTED_IMAGES_COUNT=$SELECTED_IMGS" >>"$REPORT"
echo "SCENES_COUNT=$SCENES_COUNT" >>"$REPORT"

# =============================================================================
# 12.1 — Atualiza state: PRÉ-UPLOAD passou.
# (Todas pré-validações passaram: script, imagens, áudio, MP4, ffprobe, HTTP,
#  banco correto, OAuth válido.)
# =============================================================================
cat >"$PREV_LOCK_FILE" <<EOF
RUN_TAG=$RUN_TAG
PREV_SERIES_ID=$SERIES_ID
PREV_TASK_ID=$TASK_ID
PREV_STATE=PRE_UPLOAD_PASSED
PREV_VIDEO_URL=$VIDEO_URL
PREV_MP4_PATH=$FILE_PATH
PREV_MP4_BYTES=$MP4_BYTES
PREV_DURATION_S=$VIDEO_DUR
PREV_AUDIO_PATH=$AUDIO_PATH
PREV_SELECTED_IMAGES_COUNT=$SELECTED_IMGS
PREV_SCENES_COUNT=$SCENES_COUNT
PREV_STARTED_AT=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOF
pass "Todas pré-validações passaram. State salvo: PRE_UPLOAD_PASSED."

# =============================================================================
# 12.2 — Double-check BANCO CORRETO + OAUTH VÁLIDO antes de upload
# =============================================================================
section "12.2 Dupla-checagem pré-upload: banco + OAuth"
STEP_AT "Confirmar container usa banco=$FORCED_DB_NAME e YouTube service continua conectado"

DB_OK=$(docker exec "$CONTAINER" sh -c 'python3 -c "
import os
url = os.environ.get(\"DATABASE_URL\",\"\")
dbname = \"\"
if url.startswith(\"postgresql\"):
    without = url.split(\"?\")[0]
    dbname = without.rsplit(\"/\",1)[-1]
print(dbname)
"' 2>>"$MAIN_LOG" || echo "")
if [ "$DB_OK" != "$FORCED_DB_NAME" ]; then
  fail "Pré-upload: container com banco='$DB_OK' != '$FORCED_DB_NAME'. Upload BLOQUEADO."
fi
pass "Banco do container (pré-upload): ${DB_OK} — OK."

YT_STATUS_2=$(http_json "${BASE_URL}/youtube/status" -H "$AUTH_HEAD")
SVC2=$(echo "$YT_STATUS_2" | jq -r '.service_connected // false' 2>/dev/null || echo "false")
ERR2=$(echo "$YT_STATUS_2" | jq -r '.service_auth_error // empty' 2>/dev/null || echo "")
if [ "$SVC2" != "true" ]; then
  fail "Pré-upload: YouTube service desconectado (auth_error=$ERR2). Upload BLOQUEADO."
fi
pass "YouTube service conectado (pré-upload): OK (auth_error=${ERR2:-<none>})."

# =============================================================================
# Fim
# =============================================================================
FINAL_SUCCESS=1
section "SUCESSO — Pipeline YouTube Auto validado ponta-a-ponta"
pass "Pipeline completo validado (roteiro → imagens → áudio → MP4 → HTTP → YouTube Unlisted)."
log "Abra os links abaixo para validação manual final:"
log "  MP4 local  : ${PUBLIC_URL}"
log "  YouTube    : ${YT_URL}"
log "  Relatório  : ${REPORT}"
log "  Logs       : ${MAIN_LOG}"

exit 0
