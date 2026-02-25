# Guia de Deploy no Coolify (com PostgreSQL)

Este guia coloca o sistema no Coolify usando a melhor opcao de banco para producao: **PostgreSQL**.

## 1) Criar o app no Coolify

1. No painel do Coolify, abra **Projects -> New Project**.
2. Selecione o ambiente (ex: `production`).
3. Clique em **+ New Resource**.
4. Selecione o repositorio GitHub.
5. Em build pack, mantenha **Dockerfile**.
6. Confirme o caminho: `/Dockerfile`.

## 2) Criar um banco PostgreSQL no Coolify

1. No mesmo projeto, clique em **+ New Resource** novamente.
2. Escolha **PostgreSQL**.
3. Defina nome, usuario, senha e database.
4. Salve e aguarde subir o servico.

> Recomendacao: use PostgreSQL para estabilidade, concorrencia e escalabilidade.

## 3) Configurar variaveis do app

No recurso da aplicacao (web), adicione:

| Chave | Exemplo | Descricao |
|---|---|---|
| `APP_ENV` | `production` | Modo producao |
| `PORT` | `8000` | Porta do uvicorn |
| `SECRET_KEY` | `sua-chave-secreta-forte` | JWT e seguranca |
| `BASE_URL` | `https://app.seu-dominio.com` | URL publica final |
| `ADMIN_EMAIL` | `admin@dominio.com` | Admin inicial |
| `ADMIN_PASSWORD` | `senha-forte` | Senha do admin inicial |
| `DATABASE_URL` | `postgresql://USER:PASSWORD@HOST:5432/DB` | Conexao com PostgreSQL |

Variaveis opcionais de pool para PostgreSQL:

| Chave | Padrao |
|---|---|
| `DB_POOL_SIZE` | `5` |
| `DB_MAX_OVERFLOW` | `10` |
| `DB_POOL_RECYCLE` | `1800` |

## 4) Storage/Volume

Se quiser persistir arquivos gerados (videos/capas), configure volume:

- **Destination Path**: `/data`

Com PostgreSQL, o volume nao e para banco; e apenas para arquivos gerados pela aplicacao.

## 5) Fazer deploy

1. Clique em **Deploy**.
2. Aguarde build e start.
3. Acompanhe logs em **Deployments**.

## 6) Verificar

- Health: `https://seu-dominio/health`
- Health DB: `https://seu-dominio/health/db` (deve indicar PostgreSQL)
- Acodexialista: `https://seu-dominio/acodexialista/`

## Nota sobre migracao do Render

Se a `DATABASE_URL` antiga do Render estiver invalida, atualize para a URL do PostgreSQL do Coolify.
Nao apague sem substituir, para evitar fallback para SQLite em producao.
