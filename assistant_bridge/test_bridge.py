"""Basic regression checks for the isolated production bridge.

Run after installing FastAPI, httpx, pytest and Pydantic v2:
    pytest assistant_bridge/test_bridge.py
"""
import os
import tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant_bridge.router import router


def test_review_first_flow():
    with tempfile.TemporaryDirectory() as directory:
        previous_token = os.environ.get("CODEXIA_ASSISTANT_BRIDGE_TOKEN")
        previous_db = os.environ.get("CODEXIA_ASSISTANT_BRIDGE_DB")
        try:
            os.environ["CODEXIA_ASSISTANT_BRIDGE_TOKEN"] = "test-token-" + "x" * 40
            os.environ["CODEXIA_ASSISTANT_BRIDGE_DB"] = directory + "/projects.db"
            app = FastAPI()
            app.include_router(router)
            client = TestClient(app)
            payload = {"title": "Demo", "lyrics": "Original demo lyrics",
                       "style": "Piseiro", "clip_direction": "Use licensed photographs"}
            assert client.post("/api/assistant-bridge/projects", json=payload).status_code == 401
            headers = {"Authorization": "Bearer " + os.environ["CODEXIA_ASSISTANT_BRIDGE_TOKEN"]}
            created = client.post("/api/assistant-bridge/projects", json=payload, headers=headers)
            assert created.status_code == 201
            project_id = created.json()["id"]
            assert created.json()["status"] == "awaiting_review"
            fetched = client.get(f"/api/assistant-bridge/projects/{project_id}", headers=headers)
            assert fetched.status_code == 200
            assert fetched.json()["payload"]["title"] == "Demo"
            handoff_url = f"/api/assistant-bridge/projects/{project_id}/production-payloads"
            assert client.get(handoff_url, headers=headers).status_code == 409
            approved = client.post(f"/api/assistant-bridge/projects/{project_id}/approve", headers=headers)
            assert approved.status_code == 200
            assert approved.json()["status"] == "approved_for_generation"
            handoff = client.get(handoff_url, headers=headers)
            assert handoff.status_code == 200
            assert handoff.json()["music_request"]["genre"] == "Piseiro"
            assert handoff.json()["auto_upload_youtube"] is False
            assert handoff.json()["requires_explicit_generation_confirmation"] is True
            assert client.post(f"/api/assistant-bridge/projects/{project_id}/approve", headers=headers).status_code == 409
            # A cancelled draft cannot be approved or used for production.
            cancelled_draft = client.post("/api/assistant-bridge/projects", json=payload, headers=headers)
            assert cancelled_draft.status_code == 201
            cancelled_id = cancelled_draft.json()["id"]
            cancel_url = f"/api/assistant-bridge/projects/{cancelled_id}/cancel"
            assert client.post(cancel_url, headers=headers).json()["status"] == "cancelled"
            assert client.post(cancel_url, headers=headers).status_code == 409
            assert client.post(f"/api/assistant-bridge/projects/{cancelled_id}/approve", headers=headers).status_code == 409
            assert client.get(f"/api/assistant-bridge/projects/{cancelled_id}/production-payloads", headers=headers).status_code == 409
            # A configured bridge must reject incorrect bearer tokens.
            assert client.get(f"/api/assistant-bridge/projects/{project_id}", headers={"Authorization": "Bearer invalid"}).status_code == 401

        finally:
            if previous_token is None:
                os.environ.pop("CODEXIA_ASSISTANT_BRIDGE_TOKEN", None)
            else:
                os.environ["CODEXIA_ASSISTANT_BRIDGE_TOKEN"] = previous_token
            if previous_db is None:
                os.environ.pop("CODEXIA_ASSISTANT_BRIDGE_DB", None)
            else:
                os.environ["CODEXIA_ASSISTANT_BRIDGE_DB"] = previous_db
