# Assistant Production Bridge (prototype)

This is an **isolated backend foundation**, not an enabled production feature. It accepts a structured music/video brief, stores it durably, and awaits human approval. It does not call Suno, generate images, spend credits, or publish.

## Installation

1. Confirm this is the actual repository deployed by Coolify.
2. Ensure FastAPI and Pydantic v2 are installed.
3. Mount in the existing FastAPI application: `from assistant_bridge.router import router; app.include_router(router)`.
4. Set `CODEXIA_ASSISTANT_BRIDGE_TOKEN` (random secret >=32 characters) and `CODEXIA_ASSISTANT_BRIDGE_DB` to a **persistent** database file path.
5. Protect the endpoint with existing authentication, network policy, rate limiting, and audit logs before internet exposure.
6. Add a UI inbox showing projects awaiting review. Only then connect approved projects to the existing music pipeline.

## API

- `POST /api/assistant-bridge/projects` creates a draft with title, lyrics, style, aspect ratio, clip direction, and rights metadata for assets.
- `GET /api/assistant-bridge/projects/{id}` retrieves the draft.
- `POST /api/assistant-bridge/projects/{id}/approve` marks the brief approved, **does not** start paid generation.
- `POST /api/assistant-bridge/projects/{id}/cancel` cancels.

All calls require `Authorization: Bearer <secret>`.

## Safeguards and follow-up

- Verify source/rights of real campaign photographs; reject assets without confirmed reuse rights before rendering.
- Clearly distinguish AI-generated political scenes from authentic photographs; do not imply unverified election outcomes are real.
- Add provider adapter with asynchronous jobs, retries, idempotency, cost cap, and human review.
- Add separate explicit approval for publishing; never auto-publish a generated political video.
- Add tests, migration strategy, and production deployment checks.
- A ChatGPT conversation does **not** automatically have network access to this API. A properly configured connector/plugin or approved tool must provide the actual dispatch operation.
