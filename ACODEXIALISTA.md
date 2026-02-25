# Acodexialista

Projeto inicial de uma lista de mercado moderna e inteligente para facilitar a rotina da dona de casa.

## Como acessar

- Com a API em execucao, abra:
  - `http://localhost:8000/acodexialista`
- Ou diretamente pelos estaticos:
  - `http://localhost:8000/static/acodexialista/index.html`

## Recursos entregues

- Cadastro rapido de itens (quantidade, unidade, categoria, prioridade, preco e observacoes).
- Sugestao automatica de categoria com base no nome do item.
- Sugestoes inteligentes com itens essenciais e historico de uso.
- Lista organizada por categoria, com filtros por busca, categoria e status.
- Controle de progresso de compras e previsao total de gasto.
- Compartilhamento da lista (Web Share API ou copia para area de transferencia).
- Persistencia local no navegador (localStorage).

## Rota nova no backend

- `GET /acodexialista`: retorna `app/static/acodexialista/index.html`.
