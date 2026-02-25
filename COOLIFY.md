# Guia de Deploy no Coolify (Migração do Render)

Este guia explica como colocar o Codexia para rodar no seu Coolify, substituindo o Render.

## 1. Criar o Projeto no Coolify

1.  No painel do Coolify, vá em **Projects** -> **New Project**.
2.  Escolha o ambiente (ex: `production`).
3.  Clique em **+ New Resource**.
4.  Selecione **Public Repository** (ou Private se você conectou seu GitHub).
5.  Em **Repository URL**, coloque:
    `https://github.com/evarantes/codexia-app`
6.  Clique em **Check Repository**.
7.  Mantenha o **Build Pack** como `Dockerfile`.
8.  Clique em **Continue**.

## 2. Configurações Importantes (Antes do Deploy)

Vá nas configurações do recurso que acabou de ser criado:

### General
- **Name**: Codexia (ou o que preferir)
- **Domains**: Configure o domínio que você quer usar (ex: `https://app.seudominio.com` ou use o domínio gratuito do Coolify se disponível).

### Build
- **Dockerfile Path**: `/Dockerfile` (já deve estar correto)

### Services (Networking / Ports)
- **Ports Exposes**: `8000`
  *(O Coolify deve detectar isso automaticamente do Dockerfile, mas confirme).*

### Storages (Volumes) - **MUITO IMPORTANTE**
Para não perder o banco de dados e os vídeos quando fizer deploy, você **precisa** configurar um volume persistente.

1.  Vá na aba **Storage**.
2.  Clique em **Add Storage** (ou Edit se já tiver um).
3.  Preencha:
    - **Volume Name**: `codexia-data` (ou deixe o automático)
    - **Destination Path**: `/data` (NÃO use `/data/media` aqui, use apenas `/data`)
4.  Salve.

### Environment Variables (Variáveis de Ambiente)
Vá na aba **Environment Variables** e adicione:

| Chave | Valor (Exemplo) | Descrição |
|-------|-----------------|-----------|
| `SECRET_KEY` | `sua-chave-super-secreta-aleatoria` | Segurança do JWT. Use uma string longa. |
| `BASE_URL` | `https://seu-app.coolify.dominio.com` | A URL final do seu app (sem a barra no final). |
| `ADMIN_EMAIL` | `seu@email.com` | Email para o usuário admin inicial. |
| `ADMIN_PASSWORD` | `sua-senha-forte` | Senha para o usuário admin inicial. |
| `APP_ENV` | `production` | Define modo produção. |
| `PORT` | `8000` | Garante que o uvicorn use a porta certa. |

### ⚠️ IMPORTANTE: ERRO DE LOGIN (DATABASE_URL)
Se você estiver migrando do Render, é possível que você tenha copiado a variável `DATABASE_URL` antiga.
**VOCÊ DEVE DELETAR A VARIÁVEL `DATABASE_URL` NO COOLIFY!**

- Se `DATABASE_URL` estiver definida com um endereço do Render (`postgres://...`), o sistema tentará conectar no banco antigo que não existe mais, causando **Erro 500 no Login**.
- **Solução:** Vá em Environment Variables, encontre `DATABASE_URL` e clique no ícone de lixeira para removê-la. O sistema usará automaticamente o banco local (SQLite) em `/data/vibraface.db`.

## 3. Fazer o Deploy

1.  Clique no botão **Deploy** no canto superior direito.
2.  Aguarde o build e o start.
3.  Acompanhe os logs em **Deployments**.

## 4. Verificar

Acesse a URL que você configurou.
- O login será o `ADMIN_EMAIL` e `ADMIN_PASSWORD` que você configurou.
- Seus dados do Render **não** virão automaticamente. Este é um banco novo.
- Para abrir o projeto Acodexialista, use: `https://seu-dominio/acodexialista/`
