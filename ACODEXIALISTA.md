# Acodexialista

Projeto de lista de mercado moderna e inteligente para facilitar a rotina da dona de casa.

## Onde abrir a previa

- **Local**: `http://localhost:8000/acodexialista/`
- **Coolify**: `https://SEU_DOMINIO/acodexialista/`
- **GitHub Pages** (quando habilitado): `https://SEU_USUARIO.github.io/SEU_REPOSITORIO/`

## Recursos entregues

- Cadastro rapido de itens (quantidade, unidade, categoria, prioridade, preco e observacoes).
- Sugestao automatica de categoria com base no nome do item.
- Sugestoes inteligentes com itens essenciais e historico de uso.
- Lista organizada por categoria, com filtros por busca, categoria e status.
- Controle de progresso de compras e previsao total de gasto.
- Compartilhamento da lista (Web Share API ou copia para area de transferencia).
- Persistencia local no navegador (localStorage).

## Rotas do projeto

- `GET /acodexialista` -> redireciona para `/acodexialista/`
- `GET /acodexialista/` -> preview principal da aplicacao
- `GET /acodexialista/styles.css` -> estilos da pagina
- `GET /acodexialista/app.js` -> logica da aplicacao

## Rodar no GitHub

Foram adicionados workflows em `.github/workflows`:

- `ci.yml`: valida backend/frontend em push e pull request.
- `preview-pages.yml`: publica a previa estatica no GitHub Pages (push na `main` ou manual).

### Como publicar a previa no GitHub Pages

1. No GitHub, abra **Settings > Pages**.
2. Em **Build and deployment**, selecione **GitHub Actions**.
3. Rode o workflow **Preview Acodexialista (Pages)**.
4. O link da previa sera exibido no resumo do job de deploy.

## Rodar no Coolify

1. Conecte o repositorio no Coolify usando o `Dockerfile`.
2. Exponha a porta `8000`.
3. Configure variaveis de ambiente (`SECRET_KEY`, `APP_ENV`, `PORT=8000` e demais necessarias).
4. Realize o deploy.
5. Abra `https://SEU_DOMINIO/acodexialista/` para ver a previa.
