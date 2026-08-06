#!/usr/bin/env bash
LANG=C.UTF-8
FIRST_FAIL=0
ERR_FILES=""

run_step() {
  local sn="$1" lab="$2"; shift 2
  echo "=== PASSO $sn === ($lab)"
  echo "CMD: $*"
  local ef="/tmp/dbg${sn}.err" of="/tmp/dbg${sn}.out"
  rm -f "$ef" "$of" 2>/dev/null
  set +e
  bash -c "$@" >"$of" 2>"$ef"
  local rc=$?
  set -e
  if [ $rc -ne 0 ]; then
    echo ""
    echo "=== PRIMEIRA FALHA ==="
    echo "PASSO: $sn ($lab)"
    echo "COMANDO: $*"
    echo "RETCODE: $rc"
    echo "--- STDOUT ---"
    cat "$of" 2>/dev/null || echo "(vazio)"
    echo "--- STDERR ---"
    cat "$ef" 2>/dev/null || echo "(vazio)"
    echo "--- FIM ---"
    exit $rc
  fi
  echo "STDOUT:"; cat "$of" 2>/dev/null || true
  echo "(rc=$rc) PASSO $sn OK"
  echo ""
}

echo "=== DEBUG_E2E.SH — PRIMEIRA FALHA === pwd=$(pwd) $(date -u)"
echo ""

run_step 1 "WORKDIR+mkdir LOG_DIR" '
SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "SD=$SD"
WD="${WORKDIR:-$SD}"
LD="$WD/e2e_logs"
mkdir -p "$LD"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
R="$LD/e2e_report_${ts}.txt"
M="$LD/e2e_run_${ts}.log"
touch "$R" "$M"
ls -la "$M" "$R"
'

run_step 2 "CONST+mkdir lock+idempotency" '
RT="${RUN_TAG:-$(date -u +%Y%m%d)}"
IK="e2e-homolog-ytauto-${RT}"
LH="/root/codexia-homolog-media/e2e_state"
LF="${LH}/run_${RT}.state"
mkdir -p "$LH" || true
echo "RUN_TAG=$RT"
echo "IDEMPOTENCY_KEY=$IK"
echo "LOCK_FILE=$LF"
'

run_step 3 "require_cmd: git docker curl jq ffprobe stat" '
for c in git docker curl jq ffprobe stat; do
  command -v "$c" >/dev/null 2>&1 || { echo "COMANDO FALTANTE: $c"; exit 2; }
  echo "OK_CMD: $c -> $(command -v $c)"
done
'

run_step 4 "Prot1: nomes prod NÃO são nosso target" '
CNT="codexia-homolog-ytauto-final"
REF="g8w4so4gkkgog0scsw0ogwkw-200824550318"
PP=("codexia-prod" "codexia-production" "coolify-codexia-prod" "prod")
for p in "${PP[@]}"; do
  [ "$p" = "$CNT" ] && { echo "PROT1_FAIL: CNT bate prod"; exit 3; }
  if docker ps -a --format "{{.Names}}" 2>/dev/null | grep -Fxq "$p" && [ "$p" != "$REF" ]; then echo "(info) existe $p, não tocamos"; fi
done
echo "PROT_1_OK"
'

run_step 5 "Prot2: extrair DATABASE_URL do REF_CONTAINER_HOMOLOG" '
REF="g8w4so4gkkgog0scsw0ogwkw-200824550318"
FORCE="codexia_sprint1_validation"
if ! docker ps -a --format "{{.Names}}" | grep -Fxq "$REF"; then
  echo "PROT2_FAIL: REF container $REF não encontrado em docker ps -a"
  echo "Containers encontrados:"; docker ps -a --format "{{.Names}}"
  exit 4
fi
RAW=$(docker inspect "$REF" --format "{{range .Config.Env}}{{.}}{{"\n"}}{{end}}" 2>/tmp/dbi.err | awk -F= "/^DATABASE_URL=/{$1=""; sub(/^=/,""); print; exit}" || echo "")
echo "RAW_DB len=${#RAW}"
[ -n "$RAW" ] || { echo "PROT2_FAIL: DATABASE_URL não encontrada"; cat /tmp/dbi.err 2>/dev/null; exit 5; }
WQ="${RAW%%\?*}"; QP=""
case "$RAW" in *\?*) QP="?${RAW#*\?}";; esac
N="${WQ%/*}/${FORCE}${QP}"
export DATABASE_URL="$N"
NC="${DATABASE_URL%$QP}"; NC="${NC##*/}"
[ "$NC" = "$FORCE" ] || { echo "PROT2_FAIL NAME_CHECK=[$NC] != FORCE=[$FORCE]"; exit 6; }
echo "PROT_2_OK: banco=$NC"
'

echo ""
echo "=== TODOS OS PASSOS PASSARAM. Nenhuma falha na inicialização."
echo "Próximos passos (não testados) estão no original run_e2e: git fetch/checkout, build, run..."
