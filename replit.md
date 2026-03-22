# Codexia - Content Factory

## Project Overview

Codexia (v1.1) is a comprehensive content automation platform ("Fábrica de Conteúdo") that uses AI to automate the creation, management, and distribution of digital products and marketing materials.

## Key Features

- **Book Factory:** Generates AI-authored books (structure, content, cover), exports as PDFs
- **Video Creator:** Auto-generates videos (YouTube Shorts/Reels) from text/themes
- **YouTube/Hotmart Automation:** Channel monitoring, stats analysis, sales management
- **Marketing Automation:** Ad copy and social media post generation
- **AI/Humor Factory:** Specialized modules for AI-driven and humor-based content
- **Multi-tenant SaaS:** Multiple users/tenants with roles (Admin, Client, Collaborator)

## Tech Stack

- **Backend:** FastAPI + SQLAlchemy + Uvicorn
- **Frontend:** Vue.js 3 (CDN) + Tailwind CSS + FontAwesome served via Jinja2/static files
- **Database:** PostgreSQL (via `DATABASE_URL` env var)
- **Task Queue:** Redis + RQ (background workers)
- **AI Providers:** OpenAI, Google Gemini, DeepSeek, Anthropic, Mistral, OpenRouter
- **Media:** MoviePy, Pillow, gTTS, Edge-TTS, ElevenLabs
- **Storage:** MinIO/S3 compatible

## Project Structure

```
app/
  main.py          - FastAPI entry point, startup migrations
  database.py      - DB engine and session setup
  config.py        - Centralized path/URL configuration
  models.py        - SQLAlchemy database models
  routers/         - API endpoints (books, video, auth, marketing, etc.)
  services/        - Core business logic
  modules/         - Sub-apps (ai_factory, humor_factory)
  static/          - Frontend assets (HTML, JS, CSS, media)
  worker.py        - RQ background worker entry point
alembic/           - Database migration scripts
```

## Running the Application

The app runs via the "Start application" workflow:
```
uvicorn app.main:app --host 0.0.0.0 --port 5000 --reload
```

## Environment Variables

- `DATABASE_URL` - PostgreSQL connection string (auto-set by Replit)
- `SECRET_KEY` - JWT secret key for authentication
- `ADMIN_EMAIL` / `ADMIN_PASSWORD` / `ADMIN_NAME` - Auto-creates admin user on startup
- `APP_ENV` - Set to "production" for production mode
- `CORS_ORIGINS` - Comma-separated allowed origins (default: "*")

## Deployment

Configured for autoscale deployment using Gunicorn:
```
gunicorn --bind=0.0.0.0:5000 --reuse-port -w 4 app.main:app
```
