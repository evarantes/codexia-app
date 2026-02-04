# Codexia no Coolify

## Configuração recomendada

- **Porta**: O container expõe a porta `8000`. No Coolify, configure o serviço para usar a porta **8000** (ou defina a variável de ambiente `PORT` com a porta que o Coolify mapear).
- **Variáveis de ambiente**: Defina pelo menos:
  - `DATABASE_URL` – URL do PostgreSQL (ex.: `postgresql://user:pass@host:5432/dbname`). Se o Coolify fornecer `postgres://`, o app converte para `postgresql://`.
  - Outras chaves (API keys, etc.) conforme a aba Configurações da aplicação.

## URLs após subir o app

- **`/`** – API (JSON): `{"message": "Codexia API is running"}`
- **`/app`** – Interface web (index.html)
- **`/health`** – Health check (usado pelo HEALTHCHECK do Docker)
- **`/login.html`** – Página de login

## Se o container ficar em "Restarting"

1. Abra **Logs** ou **Terminal** no Coolify e veja a saída do uvicorn e as mensagens de erro em Python.
2. Confirme que a **porta** do serviço no Coolify é a mesma que o app usa (padrão 8000).
3. Confirme que **DATABASE_URL** está definida e acessível a partir do container (rede do Postgres).
4. O HEALTHCHECK dá 60s de `start-period` para o app subir; se o startup for mais lento (muitas migrações), pode ser necessário aumentar memória/CPU ou revisar migrações.

## Build

O deploy usa o `Dockerfile` na raiz do repositório. Não é necessário usar o Procfile no Coolify quando o deploy for via Docker.
