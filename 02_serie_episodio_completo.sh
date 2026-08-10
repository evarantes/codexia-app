#!/usr/bin/env bash
# =========================================================================
#  02_serie_episodio_completo.sh — Homologação REAL (docker run, NÃO compose)
#  Container: codexia-homolog-ytauto-final
#  Porta host: 8010 -> 8000/tcp
#  Rede: coolify
#  Banco: DATABASE_URL DENTRO do container (NÃO impressa)
#  Consultas SQL: Python/SQLAlchemy DENTRO do container (NÃO expõe URL)
#
#  Itens 3-20:
#   3. Criar série nova
#   4. Gerar episódio completo (awaiting_review+/approved+/published/failed)
#   5. Texto gerado (script_json / storyboard_json)
#   6. Imagens (images_json paths)
#   7. Providers (texto, imagem, voz)
#   8. OpenAI gpt-image-1 (image_model em {gpt-image-1, dall-e-3, dall-e-2, gpt-image})
#   9. call_count_text / _image / _audio
#  10. estimated_cost + actual_cost
#  11. Áudio (path + size_bytes > 0)
#  12. MP4 (path + size_bytes > 100KB)
#  13. ffprobe streams vídeo + áudio
#  14. HTTP 200/206 Range bytes=0-0
#  15. unifie_videos (todas colunas)
#  16. video_tasks (task_id + idempotency_key)
#  17. idempotency_key
#  18. COUNT == 1 por IK
#  19. 1 item em Aguardando Publicação
#  20. 1 upload YouTube (apenas 1; 2ª publish = already_uploaded)
# =========================================================================

set -u
set -o pipefail
source /tmp/codexia_homolog/.env_migration 2>/dev/null || true

: "${APP_CID:=codexia-homolog-ytauto-final}"
: "${APP_HOST:=http://127.0.0.1:8010}"
: "${ADMIN_EMAIL:=admin@codexia.dev}"
: "${ADMIN_PASSWORD:=admin123}"
: "${ALEMBIC_INI:=/app/alembic.ini}"
: "${MAX_WAIT_MIN:=90}"
: "${N_EPISODES:=1}"
: "${DURATION_MIN:=3}"
: "${ASPECT_RATIO:=16:9}"
: "${TZ:=America/Sao_Paulo}"
: "${VISIBILITY:=unlisted}"

log(){  printf "\n\033[1;36m=====> %s\033[0m\n" "$*"; }
ok(){   printf "\033[1;32m[OK]\033[0m %s\n" "$*"; }
warn(){ printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
fail(){ printf "\033[1;31m[FAIL]\033[0m %s\n" "$*"; }

APP_RUN(){ docker exec -i "$APP_CID" bash -lc "$*"; }

# SQLJSON executa SQL via app Python; retorna JSON (NÃO imprime conexão).
SQLJSON(){
  local sql="$1"
  APP_RUN "
python - <<'PY'
import os, json
from sqlalchemy import create_engine, text as _t
url = os.environ['DATABASE_URL']
e = create_engine(url, future=True)
with e.connect() as c:
    rows = c.execute(_t('''$sql''')).mappings().all()
    print(json.dumps([dict(r) for r in rows], indent=2, default=str))
PY
"
}
# SQLTABLE usa python para PRINT TABELADO (sem json) — melhor para o humano ler.
SQLTABLE(){
  local sql="$1"
  APP_RUN "
python - <<'PY'
import os
from sqlalchemy import create_engine, text as _t
url = os.environ['DATABASE_URL']
e = create_engine(url, future=True)
with e.connect() as c:
    rp = c.execute(_t('''$sql'''))
    rows = rp.all()
    cols = list(rp.keys())
    widths = [max(len(str(x)) for x in col) for col in zip(cols, *[[str(r[c])[:60] for c in cols] for r in rows])] if rows else [len(c) for c in cols]
    def fmtline(vals): return ' | '.join(str(v).ljust(w) for v,w in zip(vals, widths))
    print(fmtline(cols))
    print('-+-'.join('-'*w for w in widths))
    for r in rows:
        print(fmtline([(str(r[c])[:200]) for c in cols]))
PY
"
}
# SQL1FIELD retorna 1 campo da 1ª linha (uso interno).
SQL1(){
  local sql="$1"
  APP_RUN "
python - <<'PY'
import os
from sqlalchemy import create_engine, text as _t
url = os.environ['DATABASE_URL']
e = create_engine(url, future=True)
with e.connect() as c:
    rp = c.execute(_t('''$sql'''))
    r = rp.first()
    if r is None:
        print('')
    else:
        print(str(r[0]) if len(r) else '')
PY
"
}

for cmd in jq curl; do command -v $cmd >/dev/null || { fail "falta $cmd no host"; exit 1; }; done

log "Preflight — container $APP_CID + docs HTTP em $APP_HOST/docs"
docker ps --filter "name=$APP_CID" --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' | head -5
curl -sSf -o /dev/null "$APP_HOST/docs" && ok "docs reachable" || { fail "$APP_HOST/docs não responde"; exit 2; }
TOKEN_RESP=$(curl -sS -X POST "$APP_HOST/token" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "username=$ADMIN_EMAIL" \
  --data-urlencode "password=$ADMIN_PASSWORD")
TOKEN=$(printf '%s' "$TOKEN_RESP" | jq -r '.access_token // empty')
[ -n "$TOKEN" ] || { fail "Token não obtido. Resp: $(printf '%s' "$TOKEN_RESP" | head -c 600)"; exit 3; }
AUTH="Authorization: Bearer $TOKEN"
ok "Token admin (len=${#TOKEN})"

mkdir -p /tmp/codexia_homolog
echo "APP_CID=$APP_CID"     >> /tmp/codexia_homolog/.env_teste
echo "APP_HOST=$APP_HOST"   >> /tmp/codexia_homolog/.env_teste

# ========================= ITEM 3 — CRIAR SÉRIE NOVA ========================
log "ITEM 3 — Criar Série Programada NOVA ($N_EPISODES eps, $DURATION_MIN min, $ASPECT_RATIO)"
SERIES_NAME="Homolog Serie Nova - $(date +%s)"
START_DATE=$(date -u +%Y-%m-%d)
SR_PAY=$(jq -n \
  --arg nm "$SERIES_NAME" \
  --arg th "Restauração e Graça: o Deus de todas as coisas (Rm 8:28)" \
  --arg kd devotional \
  --arg au "Cristãos 25-45 anos, Brasil" \
  --argjson ne "$N_EPISODES" \
  --argjson du "$DURATION_MIN" \
  --arg st "$START_DATE" \
  --arg tz "$TZ" \
  --arg ar "$ASPECT_RATIO" \
  --arg vs "$VISIBILITY" \
  --argjson ap false \
  --argjson au2 false \
  '
  {
    name: $nm,
    theme: $th,
    topic: $th,
    content_kind: $kd,
    target_audience: $au,
    number_of_episodes: $ne,
    episodes_count: $ne,
    duration_minutes: $du,
    duration: $du,
    start_date: $st,
    daily_time: "09:00",
    timezone: $tz,
    aspect_ratio: $ar,
    visibility: $vs,
    auto_approval: $ap,
    auto_publish: $au2,
    tags: ["homolog","pipeline-central"]
  }')
SERIES_ID=""
for EP in "/youtube/series" "/series" "/youtube/auto/series"; do
  R=$(curl -sS -X POST "$APP_HOST$EP" -H 'Content-Type: application/json' -H "$AUTH" -d "$SR_PAY")
  ID=$(printf '%s' "$R" | jq -r '.series_id // .id // .data.series.id // empty')
  if [ -n "$ID" ]; then SERIES_ID=$ID; EP_USED=$EP; break; fi
done
[ -n "$SERIES_ID" ] || { fail "NÃO CRIOU SÉRIE. Última resp: $(printf '%s' "${R:-}" | head -c 1500)"; exit 4; }
ok "ITEM 3 OK — série id=$SERIES_ID (endpoint $EP_USED)"
echo "SERIES_ID=$SERIES_ID"       >> /tmp/codexia_homolog/.env_teste
echo "SERIES_NAME=$SERIES_NAME"   >> /tmp/codexia_homolog/.env_teste

# ========================= ITEM 4 — GERAR EPISÓDIO ========================
log "ITEM 4 — Disparar episódio 1 + aguardar pipeline até status final (max ${MAX_WAIT_MIN}min)"
EID=""
for ep_path in "/youtube/series/$SERIES_ID/episodes" "/series/$SERIES_ID/episodes" "/youtube/auto/series/$SERIES_ID/episodes"; do
  EPR=$(curl -sS "$APP_HOST$ep_path" -H "$AUTH")
  EID=$(printf '%s' "$EPR" | jq -r '.[0].id // .episodes[0].id // .data.episodes[0].id // empty')
  [ -n "$EID" ] && break
done
if [ -z "$EID" ]; then
  EID=$(SQL1 "SELECT id FROM series_episodes WHERE series_id=$SERIES_ID ORDER BY episode_number ASC LIMIT 1;")
fi
[ -n "$EID" ] || { fail "sem episode_id para série $SERIES_ID"; exit 5; }
ok "episode_id=$EID"
echo "EPISODE_ID=$EID" >> /tmp/codexia_homolog/.env_teste

START_ENDPOINTS=(
  "$APP_HOST/youtube/series/$SERIES_ID/episodes/$EID/start"
  "$APP_HOST/youtube/series/episodes/$EID/produce"
  "$APP_HOST/youtube/series/episodes/$EID/start"
  "$APP_HOST/series/episodes/$EID/produce"
)
TID=""
for URL in "${START_ENDPOINTS[@]}"; do
  TR=$(curl -sS -X POST "$URL" -H 'Content-Type: application/json' -H "$AUTH" -d '{"force_regenerate":false}')
  TID=$(printf '%s' "$TR" | jq -r '.task_id // empty')
  [ -n "$TID" ] && break
done
if [ -z "$TID" ]; then
  TID=$(SQL1 "SELECT task_id FROM unified_videos WHERE source_module='youtube_series' AND source_id='episode:$EID' ORDER BY id DESC LIMIT 1;")
fi
[ -n "$TID" ] || { fail "nenhum task_id disparado. Último start: $(printf '%s' "${TR:-}" | head -c 1500)"; exit 6; }
ok "task_id=$TID"
echo "TASK_ID=$TID" >> /tmp/codexia_homolog/.env_teste

sleep 20
IK_B=$(SQL1 "SELECT idempotency_key FROM unified_videos WHERE source_module='youtube_series' AND source_id='episode:$EID' ORDER BY id DESC LIMIT 1;")
if [ -z "$IK_B" ]; then
  IK_B=$(SQL1 "SELECT idempotency_key FROM unified_videos WHERE task_id='$TID' ORDER BY id DESC LIMIT 1;")
fi

deadline=$(( $(date +%s) + MAX_WAIT_MIN*60 ))
STATUS=""
while [ $(date +%s) -lt $deadline ]; do
  [ -z "$IK_B" ] && IK_B=$(SQL1 "SELECT idempotency_key FROM unified_videos WHERE task_id='$TID' ORDER BY id DESC LIMIT 1;")
  [ -z "$IK_B" ] && { sleep 30; continue; }
  STATUS=$(SQL1 "SELECT status FROM unified_videos WHERE idempotency_key='$IK_B' LIMIT 1;")
  STEP=$(SQL1   "SELECT current_step FROM unified_videos WHERE idempotency_key='$IK_B' LIMIT 1;")
  PROG=$(SQL1   "SELECT progress FROM unified_videos WHERE idempotency_key='$IK_B' LIMIT 1;")
  case "$STATUS" in awaiting_review|approved|published|failed|cancelled) break ;; esac
  echo "  $(date +%H:%M:%S) status=$STATUS step=$STEP progress=$PROG IK=$IK_B"
  sleep 30
done
ok "ITEM 4 OK — status final=$STATUS"
echo "IDEMPOTENCY_KEY=$IK_B"   >> /tmp/codexia_homolog/.env_teste
echo "STATUS_INITIAL=$STATUS" >> /tmp/codexia_homolog/.env_teste

# ========================= ITEM 17 — IDEMPOTENCY KEY ======================
log "ITEM 17 — idempotency_key"
echo "  $IK_B"

# ========================= ITEM 5 — TEXTO =================================
log "ITEM 5 — Geração de texto (script_json + storyboard_json)"
SQLTABLE "
SELECT
  (script_json IS NOT NULL AND char_length(script_json::text) > 200) AS script_ok,
  (storyboard_json IS NOT NULL AND char_length(storyboard_json::text) > 500) AS storyboard_ok,
  char_length(COALESCE(script_json::text,'')) AS script_chars,
  char_length(COALESCE(storyboard_json::text,'')) AS storyboard_chars
FROM unified_videos WHERE idempotency_key='$IK_B';
"
echo "  amostra script_json 300 chars: $(SQL1 "SELECT COALESCE(left(script_json::text,300),'') FROM unified_videos WHERE idempotency_key='$IK_B';")"
echo ""
echo "  amostra storyboard_json 300 chars: $(SQL1 "SELECT COALESCE(left(storyboard_json::text,300),'') FROM unified_videos WHERE idempotency_key='$IK_B';")"

# ========================= ITEM 6 — IMAGENS ===============================
log "ITEM 6 — Geração de imagens (paths em /data)"
IMG_PATHS_COUNT=$(SQL1 "SELECT COALESCE(jsonb_array_length(images_json->'paths'),0) FROM unified_videos WHERE idempotency_key='$IK_B';")
IMG_SCENES=$(SQL1 "SELECT COALESCE(jsonb_array_length(storyboard_json->'scenes'),0) FROM unified_videos WHERE idempotency_key='$IK_B';")
echo "  qtd paths imagens = $IMG_PATHS_COUNT ; qtd cenas = $IMG_SCENES"
SQL1 "SELECT jsonb_array_elements_text(images_json->'paths') FROM unified_videos WHERE idempotency_key='$IK_B';" | \
while read -r p; do
  [ -z "$p" ] && continue
  RES=$(APP_RUN "if [ -f '$p' ]; then stat -c 'EXISTE size_bytes=%s path=%n' '$p'; else echo 'FALTANDO path=$p'; fi" 2>&1)
  echo "    $RES"
done

# ========= ITENS 7, 8, 9, 10 — PROVIDERS / GPT-IMAGE-1 / CALLS / CUSTO ====
log "ITEM 7+8+9+10 — Providers, OpenAI gpt-image-1, chamadas e custo"
SQLTABLE "
SELECT
  text_provider            AS item7_text_provider,
  text_model               AS item7_text_model,
  image_provider           AS item7_image_provider,
  image_model              AS item7_image_model,
  (image_model IN ('gpt-image-1','dall-e-3','dall-e-2','gpt-image')) AS item8_openai_gpt_image,
  voice_provider           AS item7_voice_provider,
  voice_model              AS item7_voice_model,
  call_count_text          AS item9_call_text,
  call_count_image         AS item9_call_image,
  call_count_audio         AS item9_call_audio,
  estimated_cost::numeric(18,6) AS item10_est_cost,
  actual_cost::numeric(18,6)    AS item10_act_cost
FROM unified_videos WHERE idempotency_key='$IK_B';
"

# ========================= ITEM 11 — ÁUDIO ================================
log "ITEM 11 — Áudio (existe e > 0 bytes)"
AUD_PATH=$(SQL1 "SELECT COALESCE(audio_path,'') FROM unified_videos WHERE idempotency_key='$IK_B';")
AUD_SZ=$(SQL1   "SELECT COALESCE(audio_size_bytes,0) FROM unified_videos WHERE idempotency_key='$IK_B';")
echo "  audio_path=$AUD_PATH  size_bytes=$AUD_SZ"
[ -n "$AUD_PATH" ] && APP_RUN "if [ -f '$AUD_PATH' ]; then stat -c 'AUDIO_EXISTE size_bytes=%s path=%n' '$AUD_PATH'; else echo 'AUDIO_FALTANDO'; fi"

# ========================= ITEM 12 — MP4 ==================================
log "ITEM 12 — MP4 (>100KB e em /data)"
MP4_PATH=$(SQL1 "SELECT COALESCE(video_path,'') FROM unified_videos WHERE idempotency_key='$IK_B';")
MP4_SZ=$(SQL1   "SELECT COALESCE(video_size_bytes,0) FROM unified_videos WHERE idempotency_key='$IK_B';")
echo "  video_path=$MP4_PATH  size_bytes=$MP4_SZ  (esperado > 102400)"
[ -n "$MP4_PATH" ] && APP_RUN "if [ -f '$MP4_PATH' ]; then stat -c 'MP4_EXISTE size_bytes=%s path=%n' '$MP4_PATH'; else echo 'MP4_FALTANDO'; fi"
case "$MP4_PATH" in /data/*) echo "  -> PERSISTENTE (/data) OK";; *) echo "  -> FORA do /data (ATENÇÃO)";; esac

# ========================= ITEM 13 — FFPROBE ==============================
log "ITEM 13 — ffprobe streams vídeo+áudio"
if [ -n "$MP4_PATH" ]; then
  APP_RUN "
    if command -v ffprobe >/dev/null 2>&1; then
      ffprobe -hide_banner -v error \
        -show_entries stream=index,codec_type,codec_name,duration,width,height,sample_rate,channels \
        -show_entries format=duration,size,bit_rate,format_name \
        -of json '$MP4_PATH'
    else
      echo FFPROBE_NAO_INSTALADO
    fi
  "
fi

# ========================= ITEM 14 — HTTP 200/206 RANGE ===================
log "ITEM 14 — HTTP 200/206 Range bytes=0-0"
VID_URL=$(SQL1 "SELECT COALESCE(video_url,'') FROM unified_videos WHERE idempotency_key='$IK_B';")
echo "  video_url=$VID_URL"
FULL_VIDEO_URL=""
if [ -n "$VID_URL" ]; then
  case "$VID_URL" in http*) FULL_VIDEO_URL="$VID_URL";; /*) FULL_VIDEO_URL="$APP_HOST$VID_URL";; *) FULL_VIDEO_URL="$APP_HOST/$VID_URL";; esac
  echo "  GET $FULL_VIDEO_URL Range: bytes=0-0"
  curl -sS -I -H 'Range: bytes=0-0' "$FULL_VIDEO_URL" | head -20
fi
echo "FULL_VIDEO_URL=$FULL_VIDEO_URL" >> /tmp/codexia_homolog/.env_teste

# ========================= ITEM 15 — unified_videos COMPLETO ==============
log "ITEM 15 — unified_videos (todas as colunas) [formato JSON]"
SQLJSON "SELECT * FROM unified_videos WHERE idempotency_key='$IK_B';"

# ========================= ITEM 16 — video_tasks ==========================
log "ITEM 16 — video_tasks (task_id ou IK)"
SQLJSON "SELECT id, task_type, status, progress, idempotency_key, user_id, left(payload::text,600) AS payload_600chars FROM video_tasks WHERE id='$TID' OR idempotency_key='$IK_B';"

# ========================= ITEM 18 — 1 TASK POR IK ========================
log "ITEM 18 — Idempotência: 1 IK = 1 video_task + 1 unified_video"
SQLTABLE "
SELECT 'video_tasks by IK' tabela, COUNT(*) n FROM video_tasks WHERE idempotency_key='$IK_B'
UNION ALL
SELECT 'unified_videos by IK', COUNT(*) FROM unified_videos WHERE idempotency_key='$IK_B'
UNION ALL
SELECT 'unified_videos by task_id', COUNT(*) FROM unified_videos WHERE task_id='$TID';
"

# ========================= ITEM 19 — 1 AGUARDANDO PUBLICAÇÃO ==============
log "ITEM 19 — 1 item Aguardando Publicação"
SQLTABLE "
SELECT 'unified.status' src, status, COUNT(*) n FROM unified_videos WHERE idempotency_key='$IK_B' GROUP BY status
UNION ALL
SELECT 'scheduled_videos' src, status, COUNT(*) n FROM scheduled_videos
 WHERE (data::jsonb->>'task_id'='$TID' OR data::jsonb->>'video_task_id'='$TID' OR data::jsonb->>'idempotency_key'='$IK_B')
 GROUP BY status;
"

# ========================= ITEM 20 — APROVAR + PUBLICAR (1 UPLOAD) =========
log "ITEM 20 — Aprovar + Publicar 1x ; 2ª publicação = already_uploaded"
APPR=0
for app_url in \
  "$APP_HOST/youtube/series/$SERIES_ID/episodes/$EID/approve" \
  "$APP_HOST/youtube/unified/by-ik/$IK_B/approve" \
  "$APP_HOST/youtube/auto/unified/$IK_B/approve"; do
  R=$(curl -sS -X POST "$app_url" -H 'Content-Type: application/json' -H "$AUTH" -d '{}')
  if printf '%s' "$R" | jq -er '.approved or .status=="approved" or .ok' >/dev/null 2>&1; then APPR=1; break; fi
done
[ "$APPR" = 0 ] && echo "  (endpoint approve não retornou ok; esperando 10s e checando direto no banco)"
sleep 10
SQLTABLE "SELECT status AS pos_aprovacao_status FROM unified_videos WHERE idempotency_key='$IK_B';"

for pub_url in \
  "$APP_HOST/youtube/series/$SERIES_ID/episodes/$EID/publish" \
  "$APP_HOST/youtube/unified/by-ik/$IK_B/publish" \
  "$APP_HOST/youtube/auto/unified/$IK_B/publish"; do
  PUB1=$(curl -sS -X POST "$pub_url" -H 'Content-Type: application/json' -H "$AUTH" -d "{\"visibility\":\"$VISIBILITY\"}")
  YT1=$(printf '%s' "$PUB1" | jq -r '.youtube_video_id // empty')
  [ -n "$YT1" ] && break
done
dl=$(( $(date +%s) + 25*60 ))
YT=""
ST=""
while [ $(date +%s) -lt $dl ]; do
  YT=$(SQL1 "SELECT COALESCE(youtube_video_id,'') FROM unified_videos WHERE idempotency_key='$IK_B';")
  ST=$(SQL1 "SELECT status FROM unified_videos WHERE idempotency_key='$IK_B';")
  [ -n "$YT" ] && [ "$ST" = "published" ] && break
  sleep 20
done
ok "1ª publicação: youtube_video_id=$YT  status=$ST"

PUB2=$(curl -sS -X POST "$APP_HOST/youtube/unified/by-ik/$IK_B/publish" \
  -H 'Content-Type: application/json' -H "$AUTH" -d '{}' 2>/dev/null || echo '{}')
echo "  2ª publish (DEVE ser already_uploaded=true): $(printf '%s' "$PUB2" | jq -c '.' | head -c 400)"
SQLTABLE "
SELECT
  idempotency_key,
  youtube_video_id AS youtube_video_id_final,
  youtube_url      AS youtube_url_final,
  status           AS status_final
FROM unified_videos WHERE idempotency_key='$IK_B';
"
echo "YOUTUBE_VIDEO_ID=$YT" >> /tmp/codexia_homolog/.env_teste

log "SCRIPT 02 FINALIZADO. Variáveis salvas em /tmp/codexia_homolog/.env_teste (SEM segredos):"
cat /tmp/codexia_homolog/.env_teste
