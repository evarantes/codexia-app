#!/usr/bin/env bash
# =========================================================================
#  01_migracao_alembic.sh — Ambiente HOMOLOGAÇÃO REAL (docker run, NÃO compose)
#  Container alvo: codexia-homolog-ytauto-final
#  Rede: coolify
#  Banco: codexia_sprint1_validation (DATABASE_URL DENTRO do container, NÃO impressa)
#
#  ITENS 1 e 2:
#    1. Executar a migration Alembic (upgrade head)
#    2. Validar heads / current / upgrade head + tabela/índices/FKs no PostgreSQL
#
#  NÃO imprime DATABASE_URL, usuário ou senha.
#  Consultas SQL via Python/SQLAlchemy DENTRO do container (mesma conexão do app).
# =========================================================================
set -u
set -o pipefail

APP_CID="codexia-homolog-ytauto-final"
ALEMBIC_INI="${ALEMBIC_INI:-/app/alembic.ini}"

log(){  printf "\n\033[1;36m=====> %s\033[0m\n" "$*"; }
ok(){   printf "\033[1;32m[OK]\033[0m %s\n" "$*"; }
warn(){ printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
fail(){ printf "\033[1;31m[FAIL]\033[0m %s\n" "$*"; }

# --- Helpers (NÃO compose, NÃO psql no host, NÃO imprime secrets) --------
APP_RUN()  { docker exec -i "$APP_CID" bash -lc "$*"; }
APP_RUN_T(){ docker exec -it "$APP_CID" bash -lc "$*"; }

# SQL_RUN executa SQL via Python/SQLAlchemy DENTRO do container, usando a
# mesma DATABASE_URL do runtime do app (NÃO precisa de host externo, NÃO imprime).
SQL_RUN(){
  local sql="$1"
  APP_RUN "
python - <<'PY'
import os, json
from sqlalchemy import create_engine, text as _t
url = os.environ['DATABASE_URL']
engine = create_engine(url, future=True)
with engine.connect() as c:
    rows = c.execute(_t('''$sql''')).mappings().all()
    out = [dict(r) for r in rows]
    print(json.dumps(out, indent=2, default=str))
PY
"
}

log "Preflight — container $APP_CID está rodando?"
docker ps --filter "name=$APP_CID" --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}" | head -5
if ! docker exec "$APP_CID" true >/dev/null 2>&1; then
  fail "Container $APP_CID não está acessível via docker exec. Confira 'docker ps'."
  exit 2
fi
ok "Container acessível."

# =========================================================================
# Item 1 (antes) + Item 2a — alembic heads (antes do upgrade)
# =========================================================================
log "ITEM 2a — alembic heads (ANTES do upgrade)"
APP_RUN "alembic -c '$ALEMBIC_INI' heads" 2>&1 | head -30 || true

log "ITEM 2b — alembic current (ANTES do upgrade)"
APP_RUN "alembic -c '$ALEMBIC_INI' current" 2>&1 | head -30 || true

# =========================================================================
# Item 1 — Executar Alembic upgrade head
# =========================================================================
log "ITEM 1 — alembic -c $ALEMBIC_INI upgrade head"
UP_OUT=$(APP_RUN "alembic -c '$ALEMBIC_INI' upgrade head" 2>&1)
printf '%s\n' "$UP_OUT" | head -80

log "ITEM 2c — alembic current (APÓS upgrade head)"
APP_RUN "alembic -c '$ALEMBIC_INI' current" 2>&1 | head -30

log "ITEM 2d — alembic heads (APÓS upgrade head)"
APP_RUN "alembic -c '$ALEMBIC_INI' heads" 2>&1 | head -30

# =========================================================================
# Confirmação PostgreSQL da tabela unified_videos / índices / FKs
# TUDO rodando Python DENTRO do container (sem expor URL de conexão).
# =========================================================================
log "Confirmação PostgreSQL: unified_videos existe + UNIQUE IK + índices + FKs"

SQL_RUN "
SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('unified_videos','video_tasks','users','series_episodes','scheduled_videos') ORDER BY table_name;
"

SQL_RUN "
SELECT indexname, indexdef FROM pg_indexes WHERE tablename='unified_videos' ORDER BY indexname;
"

SQL_RUN "
SELECT
  c.conname,
  c.contype,
  CASE c.contype WHEN 'p' THEN 'PRIMARY' WHEN 'u' THEN 'UNIQUE' WHEN 'f' THEN 'FOREIGN' ELSE c.contype::text END tipo,
  pg_get_constraintdef(c.oid) AS definicao
FROM pg_constraint c
JOIN pg_class t ON t.oid = c.conrelid
WHERE t.relname='unified_videos' AND c.contype IN ('p','u','f')
ORDER BY c.contype, c.conname;
"

# Persiste status para scripts seguintes (NÃO contém segredos)
HEAD_ATUAL=$(APP_RUN "alembic -c '$ALEMBIC_INI' heads 2>/dev/null | awk '{print \$1}' | head -1")
mkdir -p /tmp/codexia_homolog
{
  echo "APP_CID=$APP_CID"
  echo "ALEMBIC_INI=$ALEMBIC_INI"
  echo "ALEMBIC_HEAD_ATUAL=$HEAD_ATUAL"
  echo "DATABASE_SANDBOX_CODENAME=codexia_sprint1_validation"
  echo "EXECUTADO_EM=$(date -Iseconds)"
} > /tmp/codexia_homolog/.env_migration
ok "Migration finalizada. Status salvo em /tmp/codexia_homolog/.env_migration (SEM segredos)."
cat /tmp/codexia_homolog/.env_migration
