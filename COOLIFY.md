# Codexia no Coolify

## Configuração recomendada

- **Porta**: O container expõe a porta `8000`. No Coolify, configure o serviço para usar a porta **8000** (ou defina a variável de ambiente `PORT` com a porta que o Coolify mapear).
- **Volumes**: Monte um volume em **`/data`** para persistir SQLite, backups e vídeos gerados (`/data/media/videos`).
- **Variáveis de ambiente** (obrigatórias em produção):

  | Variável | Descrição |
  |----------|-----------|
  | `SECRET_KEY` | Chave para JWT (em produção não use a padrão) |
  | `BASE_URL` | URL pública (ex.: `https://seu-dominio.sslip.io`) |
  | `ADMIN_EMAIL` | Email do admin master (criado na 1ª inicialização) |
  | `ADMIN_PASSWORD` | Senha do admin master |

  **Opcionais:**
  - `DATABASE_URL` – Se não definir, usa SQLite em `/data/vibraface.db`. Para Postgres: `postgresql://user:pass@host:5432/dbname`
  - `ADMIN_NAME` – Nome do admin (opcional)
  - `APP_ENV` – `production` (padrão) ou `development`
  - `CORS_ORIGINS` – Origens permitidas, separadas por vírgula (ex.: `https://app.exemplo.com`)
  - `ALLOW_DEBUG_ROUTES=true` – Habilita `/debug-reset-user` (evitar em produção)
- **SQLite (sem DATABASE_URL)**: O app cria a pasta `/data` se não existir e grava o banco em `/data/vibraface.db`. Na primeira execução, se existir `/app/vibraface.db` e não existir `/data/vibraface.db`, o arquivo é copiado para `/data` (migração). **Monte um volume em `/data`** no Coolify para persistir entre deploys.

## URLs após subir o app

- **`/`** – Frontend Vue (painel Codexia)
- **`/app`** – Mesmo que `/` (compatibilidade)
- **`/api/status`** – Status da API em JSON: `{"message": "Codexia API is running"}`
- **`/health`** – Health check (usado pelo HEALTHCHECK do Docker)
- **`/login.html`** – Página de login

## Se o container ficar em "Restarting"

1. Abra **Logs** ou **Terminal** no Coolify e veja a saída do uvicorn e as mensagens de erro em Python.
2. Confirme que a **porta** do serviço no Coolify é a mesma que o app usa (padrão 8000).
3. Confirme que **DATABASE_URL** está definida e acessível a partir do container (rede do Postgres).
4. O HEALTHCHECK dá 60s de `start-period` para o app subir; se o startup for mais lento (muitas migrações), pode ser necessário aumentar memória/CPU ou revisar migrações.

## Build e deploy

O deploy usa o `Dockerfile` na raiz. Não é necessário usar o Procfile no Coolify quando o deploy for via Docker.

## Backup do SQLite

- Backups automáticos em `/data/backups` (1x/dia, às 03:00). Mantém os últimos 7.
- Listar: `GET /admin/backups` (requer token admin)
- Baixar: `GET /admin/backups/{filename}` (requer token admin)
