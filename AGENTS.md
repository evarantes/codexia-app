# AGENTS.md

## Cursor Cloud specific instructions

### Overview
Codexia is a FastAPI-based content automation SaaS platform ("Fábrica de Conteúdo"). It uses AI to generate books, videos, marketing content. The frontend is Vue.js 3 served as static files from `app/static/`.

### Running the dev server
```bash
export ADMIN_EMAIL="admin@codexia.dev" ADMIN_PASSWORD="admin123" ADMIN_NAME="Admin Dev"
export SECRET_KEY="dev-secret-key-codexia-2025" APP_ENV="development"
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Key notes
- **Database**: Without `DATABASE_URL`, the app falls back to SQLite (`vibraface.db` in the workspace root). This is sufficient for local development and testing.
- **Redis**: Without `REDIS_URL`, Redis/RQ initialization fails gracefully and a `MockQueue` runs tasks synchronously. Background worker features (video rendering) are degraded but CRUD/UI works fine.
- **System deps**: `ffmpeg` (pre-installed) and `imagemagick` are required for video/image processing. ImageMagick must be installed via `apt-get install -y imagemagick`.
- **Auth**: The `/token` endpoint accepts `application/x-www-form-urlencoded` with `username` (email) and `password` fields (OAuth2 password flow). It returns a JWT bearer token.
- **Admin auto-creation**: On startup, if `ADMIN_EMAIL` and `ADMIN_PASSWORD` env vars are set, an admin user is auto-created or updated.
- **No lint/test framework**: The project does not include pytest, flake8, ruff, or any other lint/test tools in `requirements.txt`. The `tests/` directory contains only a sample PDF output, no test files.
- **Frontend**: Vue.js 3 + Tailwind CSS loaded from CDN. Served as static HTML from `app/static/index.html`. No build step required.
- **Book creation endpoint** (`POST /books/`): Uses multipart form data (`Form(...)` fields), not JSON body.
- **OpenAPI docs**: Available at `http://localhost:8000/docs` (Swagger UI) and `http://localhost:8000/redoc`.
