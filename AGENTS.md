# AGENTS.md

## Cursor Cloud specific instructions

### Project overview

Codexia is a Brazilian Portuguese AI-powered content factory SaaS (FastAPI + Vue.js SPA). The codebase has two runtime targets sharing the same Python code: the **API server** (FastAPI/Uvicorn on port 8000) and a **background worker** (RQ/Redis). The frontend is a pre-built Vue.js SPA served as static files from `app/static/`.

### Running the dev server

```bash
ADMIN_EMAIL=admin@codexia.dev ADMIN_PASSWORD=admin123 APP_ENV=development SECRET_KEY=dev_secret_key_12345 \
  python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

- Use `python3 -m uvicorn` (not bare `uvicorn`) since the executable may not be on `$PATH`.
- Without `DATABASE_URL`, the app falls back to **SQLite** (`vibraface.db` in the working directory), which is sufficient for local development.
- Set `ADMIN_EMAIL` and `ADMIN_PASSWORD` to bootstrap an admin user on startup.
- Set `SECRET_KEY` to any non-empty value for JWT signing.
- Set `APP_ENV=development` for debug mode.

### Key endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /` | Vue.js frontend (login redirects here) |
| `GET /health` | Health check (no DB) |
| `GET /api/status` | API status JSON |
| `POST /token` | OAuth2 login (form: `username` + `password`) |
| `GET /auth/me` | Current authenticated user |
| `GET /health/db` | Database connectivity check |

### System dependencies

Required system packages (already installed in the VM snapshot): `ffmpeg`, `imagemagick`, `build-essential`, `python3-dev`, `libsm6`, `libxext6`, `libgl1`, `libglib2.0-0`. The ImageMagick policy file must allow read/write for PDF operations (`sed -i 's/none/read,write/g' /etc/ImageMagick-6/policy.xml`).

### Important gotchas

- The `google-generativeai` package emits a `FutureWarning` at import time about being deprecated in favor of `google.genai`. This is harmless and does not affect functionality.
- The app runs inline database migrations on startup (not via Alembic CLI), so you do not need to manually run migration commands.
- Redis is only needed for the RQ worker (`app/worker.py`). The API server starts and operates fine without Redis for most features.
- There are no automated tests or linting configuration in the repository.
- The frontend is a single `index.html` file using CDN-loaded Vue 3 and Tailwind CSS — no build step required.
