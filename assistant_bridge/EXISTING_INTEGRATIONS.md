# Reuse inventory — confirmed from main branch

The deployed repository is `evarantes/codexia-app`, branch `main`, per Coolify.

Existing components:

| Existing file | Reusable feature |
|---|---|
| `app/main.py` | FastAPI app, includes `app.routers.music.router` |
| `app/routers/music.py` | `POST /music/generate` (Suno if configured), `GET /music/task/{id}`, `POST /music/clip/task`, `GET /music/tasks`, saved music library, image generation |
| `app/services/suno_service.py` | Suno task creation, polling, audio download |
| `app/static/pages/music-clip/index.html` | V2 menu wrapper over legacy UI |
| `app/static/legacy/index.html` | Full existing music/clip UI |

The assistant bridge does **not** need to duplicate Suno logic. `codexia_adapter.py` converts an approved project into existing request shapes. It intentionally does not execute the calls because those require authenticated user context and an explicit cost approval.

## Fast integration path

1. Mount both bridge routers in `app/main.py` and protect the UI with Codexia login; avoid deploying the prototype token prompt to the open internet.
2. Add a navigation item for `/assistant-production` in the V2 UI.
3. Replace prototype SQLite with the application's existing PostgreSQL session/model and per-user ownership.
4. Add a server-side execution endpoint with explicit `approve_generation`, budget limit, idempotency key, and queue-backed orchestration. Invoke the existing music service via a properly authenticated service layer, **not** by spoofing users or posting a bearer token from the browser.
5. After `/music/task/{id}` completes, save the audio and call `/music/clip/task` with the actual `music_filename`. Persist step state, retries, and failure reasons.
6. Integrate user-uploaded licensed photographs into the existing clip renderer; the current adapter does not pass photos to the renderer.
7. Register a dedicated ChatGPT connector/action with user consent. This is required for actual automatic dispatch from a ChatGPT conversation.
8. Test locally, CI, and staging; merge/deploy only after confirming no regressions.

## Existing payloads

`POST /music/generate`: `{title, lyrics, genre, vocal_gender}` (male `m`, female `f`).

`POST /music/clip/task`: `{title, lyrics, music_filename, author_text, watermark_enabled, sync_mode, captions_enabled, aspect_ratio, auto_upload_youtube}`.

**Important:** Existing Suno endpoint falls back to instrumental if no Suno key. For projects requiring sung vocals, fail closed when the key is missing rather than silently switching to instrumental.

## Election media integrity

Use rights-cleared real campaign photos with source credits. Do not depict an unconfirmed election outcome as actual footage. Synthetic or staged scenes should be labeled, and publishing should require a separate human approval.
