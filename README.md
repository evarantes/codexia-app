# Acodexialista

Lista de mercado inteligente e moderna para facilitar a vida da dona de casa.

## Previa

- Local: `http://localhost:8000/acodexialista/`
- Coolify: `https://SEU_DOMINIO/acodexialista/`
- GitHub Pages: habilite o workflow `Preview Acodexialista (Pages)` e use a URL gerada.

## Rodar localmente

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Abra `http://localhost:8000/acodexialista/`.

## GitHub Actions

- `CI` (`.github/workflows/ci.yml`): valida sintaxe Python/JavaScript e rotas essenciais.
- `Preview Acodexialista (Pages)` (`.github/workflows/preview-pages.yml`): publica previa estatica no GitHub Pages.

## Coolify

Use o `Dockerfile` do repositorio:

1. Build pack: `Dockerfile`
2. Porta exposta: `8000`
3. Variaveis: `APP_ENV=production`, `PORT=8000`, `SECRET_KEY` (e demais necessarias)
4. Deploy e acesso: `https://SEU_DOMINIO/acodexialista/`

Mais detalhes em `COOLIFY.md`.
