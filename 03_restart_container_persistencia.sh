#!/usr/bin/env bash
# =========================================================================
#  03_restart_container_persistencia.sh — Homolog REAL (docker run, NÃO compose)
#  Container alvo (ÚNICO A SER REINICIADO): codexia-homolog-ytauto-final
#
#  Itens 21-23:
#   21. Reiniciar SOMENTE o container app (docker restart codexia-homolog-ytauto-final)
#       — NÃO toca no PostgreSQL nem Redis.
#   22. Confirmar que arquivos continuam em /data (imagens, áudio, MP4)
#   23. Confirmar scheduler / NÃO recriação de vídeo para o mesmo episódio.
# =========================================================================
set -u
set -o pipefail
source /tmp/codexia_homolog/.env_teste     2>/dev/null || true
source /tmp/codexia_homolog/.env_migration 2>/dev/null || true

: "${APP_CID:=codexia-homolog-ytauto-final}"
: "${APP_HOST:=http://127.0.0.1:8010}"
: "${ADMIN_EMAIL:=admin@codexia.dev}"
: "${ADMIN_PASSWORD:=admin123}"

log(){  printf "\n\033[1;36m=====> %s\033[0m\n" "$*"; }
ok(){   printf "\033[1;32m[OK]\033[0m %s\n" "$*"; }
warn(){ printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
fail(){ printf "\033[1;31m[FAIL]\033[0m %s\n" "$*"; }
APP_RUN(){ docker exec -i "$APP_CID" bash -lc "$*"; }

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
    widths = [max(len(str(x)) for x in col) for col in zip(cols, *[[str(r[c])[:80] for c in cols] for r in rows])] if rows else [len(c) for c in cols]
    def fmtline(vals): return ' | '.join(str(v).ljust(w) for v,w in zip(vals, widths))
    print(fmtline(cols))
    print('-+-'.join('-'*w for w in widths))
    for r in rows:
        print(fmtline([(str(r[c])[:200]) for c in cols]))
PY
"
}
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
    if r is None: print(''); else: print(str(r[0]) if len(r) else '')
PY
"
}

if [ -z "${SERIES_ID:-}" ] || [ -z "${EPISODE_ID:-}" ] || [ -z "${IDEMPOTENCY_KEY:-}" ]; then
  fail "Falta /tmp/codexia_homolog/.env_teste com SERIES_ID EPISODE_ID IDEMPOTENCY_KEY. Rode 02 ANTES."
  echo "Conteúdo .env_teste:"; cat /tmp/codexia_homolog/.env_teste 2>/dev/null || echo "(arquivo não existe)"; exit 2
fi

# ===============================================================
# ITEM 21 — REINICIAR APENAS o container do app (NÃO postgres/redis)
# ===============================================================
log "ITEM 21 — Reiniciar container $APP_CID (APP apenas) via docker restart $APP_CID"
echo "  docker ps ANTES:"
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}" | head -6
echo ""
echo "  -> docker restart $APP_CID ..."
docker restart "$APP_CID" 2>&1 | head -5
sleep 8
echo ""
echo "  docker ps DEPOIS (status recente):"
docker ps --filter "name=$APP_CID" --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
echo ""
echo "  aguardando $APP_HOST/docs (até 150s)..."
for i in $(seq 1 15); do
  if curl -sSf -o /dev/null "$APP_HOST/docs"; then ok "App voltou em $APP_HOST/docs (tentativa $i)"; break; fi
  sleep 10; echo "    ... tentativa $i/15"
done

# ===============================================================
# ITEM 22 — Arquivos em /data persistem
# ===============================================================
log "ITEM 22 — Arquivos em /data (persistência: imagens / áudio / MP4)"
echo "  --- unified_videos (estado pós-restart) ---"
SQLTABLE "
SELECT
  left(video_path,160)  AS video_path,
  video_size_bytes      AS video_size,
  left(audio_path,160)  AS audio_path,
  audio_size_bytes      AS audio_size,
  jsonb_array_length(images_json->'paths') AS qtd_paths_imagens,
  left(video_url,160)   AS video_url,
  youtube_video_id,
  youtube_url,
  status
FROM unified_videos WHERE idempotency_key='$IDEMPOTENCY_KEY';
"
echo ""
echo "  --- /data/media completo (arquivos persistentes) ---"
APP_RUN "find /data/media -type f -mmin -1440 2>/dev/null | sort"
echo ""
echo "  --- existência individual das imagens (paths em images_json) ---"
SQL1 "SELECT jsonb_array_elements_text(images_json->'paths') FROM unified_videos WHERE idempotency_key='$IDEMPOTENCY_KEY';" | \
while read -r p; do
  [ -z "$p" ] && continue
  RES=$(APP_RUN "if [ -f '$p' ]; then stat -c 'EXISTE size_bytes=%s path=%n' '$p'; else echo 'FALTANDO path=$p'; fi" 2>&1)
  echo "    $RES"
done
AUD_PATH=$(SQL1 "SELECT COALESCE(audio_path,'') FROM unified_videos WHERE idempotency_key='$IDEMPOTENCY_KEY' LIMIT 1;")
MP4_PATH=$(SQL1 "SELECT COALESCE(video_path,'') FROM unified_videos WHERE idempotency_key='$IDEMPOTENCY_KEY' LIMIT 1;")
[ -n "$AUD_PATH" ] && APP_RUN "if [ -f '$AUD_PATH' ]; then stat -c 'AUDIO_EXISTE size_bytes=%s path=%n' '$AUD_PATH'; else echo 'AUDIO_FALTANDO'; fi"
[ -n "$MP4_PATH" ] && APP_RUN "if [ -f '$MP4_PATH' ]; then stat -c 'MP4_EXISTE size_bytes=%s path=%n' '$MP4_PATH'; else echo 'MP4_FALTANDO'; fi"
echo ""
echo "  --- HTTP Range 200/206 pós-restart: ${FULL_VIDEO_URL:-indisponível} ---"
if [ -n "${FULL_VIDEO_URL:-}" ]; then
  curl -sS -I -H 'Range: bytes=0-0' "$FULL_VIDEO_URL" | head -15
fi

# ===============================================================
# ITEM 23 — Scheduler NÃO recria vídeo para o mesmo episódio
# ===============================================================
log "ITEM 23 — Scheduler / sync NÃO recriam vídeo (idempotência pós-restart)"
COUNT_ANTES=$(SQL1 "SELECT COUNT(*) FROM unified_videos WHERE source_id='episode:$EPISODE_ID';")
echo "  unified_videos source_id=episode:$EPISODE_ID ANTES dos triggers = $COUNT_ANTES"
TOKEN=$(curl -sS -X POST "$APP_HOST/token" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "username=$ADMIN_EMAIL" \
  --data-urlencode "password=$ADMIN_PASSWORD" | jq -r '.access_token // empty')
AUTH="Authorization: Bearer $TOKEN"
for endpoint in \
  "$APP_HOST/youtube/series/sync" \
  "$APP_HOST/youtube/series/$SERIES_ID/sync" \
  "$APP_HOST/youtube/auto/sync" \
  "$APP_HOST/jobs/sync" \
  "$APP_HOST/youtube/tasks/process" \
  "$APP_HOST/series/sync" ; do
  echo -n "  POST $endpoint -> "
  OUT=$(curl -sS -X POST "$endpoint" -H 'Content-Type: application/json' -H "$AUTH" -d '{}' 2>/dev/null || echo '{}')
  printf '%s' "$OUT" | head -c 300; echo
done
echo ""
echo "  Disparando 3x generate_video aleatório (NÃO deve afetar o episódio $EPISODE_ID)..."
for i in 1 2 3; do
  R=$(curl -sS -X POST "$APP_HOST/youtube/generate_video" \
    -H 'Content-Type: application/json' -H "$AUTH" \
    -d '{"mode":"topic","topic":"Homolog scheduler pos-restart '$(date +%N)'","duration":1,"image_mode":"multiple","custom_image_count":2,"kind":"devotional","aspect_ratio":"16:9","visibility":"unlisted","review_required":true}' 2>/dev/null || echo '{}')
  echo "    #$i: $(printf '%s' "$R" | jq -c '{task_id, idempotency_key, reused_existing_task, reused_completed_task, pipeline}' 2>/dev/null | head -c 350)"
done
sleep 10
COUNT_DEPOIS=$(SQL1 "SELECT COUNT(*) FROM unified_videos WHERE source_id='episode:$EPISODE_ID';")
echo ""
echo "  unified_videos(source_id=episode:$EPISODE_ID) DEPOIS dos triggers = $COUNT_DEPOIS"
if [ "$COUNT_ANTES" = "$COUNT_DEPOIS" ]; then
  ok "ITEM 23 OK: scheduler NÃO recriou vídeo para episódio $EPISODE_ID (antes=$COUNT_ANTES, depois=$COUNT_DEPOIS)"
else
  fail "ITEM 23 FALHOU: DUPLICATA (antes=$COUNT_ANTES, depois=$COUNT_DEPOIS)"
fi
echo ""
echo "  --- estado final episódio ---"
SQLTABLE "
SELECT
  source_id,
  idempotency_key,
  status,
  youtube_video_id,
  (video_path IS NOT NULL) tem_mp4,
  (audio_path IS NOT NULL) tem_audio,
  task_id
FROM unified_videos
WHERE source_id='episode:$EPISODE_ID'
   OR task_id IN (
      SELECT DISTINCT task_id FROM unified_videos WHERE idempotency_key='$IDEMPOTENCY_KEY'
   )
ORDER BY id;
"
log "SCRIPT 03 CONCLUÍDO"
