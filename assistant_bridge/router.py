"""Assistant -> Codexia production inbox (opt-in FastAPI router).

Mount explicitly in the main application:
    from assistant_bridge.router import router
    app.include_router(router)

Set CODEXIA_ASSISTANT_BRIDGE_TOKEN to a long random secret and
CODEXIA_ASSISTANT_BRIDGE_DB to a persistent SQLite path.
This module NEVER publishes or calls paid providers automatically.
"""
from __future__ import annotations

import hmac
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/assistant-bridge", tags=["assistant-bridge"])


def authorize(authorization: str | None = Header(default=None)) -> None:
    secret = os.environ.get("CODEXIA_ASSISTANT_BRIDGE_TOKEN", "")
    if len(secret) < 32:
        raise HTTPException(503, "Assistant bridge is not configured")
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not hmac.compare_digest(supplied, secret):
        raise HTTPException(401, "Unauthorized")


def database() -> sqlite3.Connection:
    location = Path(os.environ.get("CODEXIA_ASSISTANT_BRIDGE_DB", "/data/assistant_bridge.sqlite3"))
    location.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(location), timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS assistant_projects (
        id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, status TEXT NOT NULL,
        payload TEXT NOT NULL
    )""")
    return db


class MediaAsset(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    source: str = Field(min_length=1, max_length=256)
    rights_confirmed: bool = False
    caption: str = Field(default="", max_length=500)


class ProductionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    lyrics: str = Field(min_length=1, max_length=30000)
    style: str = Field(min_length=1, max_length=2000)
    language: str = "pt-BR"
    voice: str = "automatic"
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    clip_direction: str = Field(default="", max_length=10000)
    assets: list[MediaAsset] = Field(default_factory=list, max_length=100)
    synthetic_media_disclosure: bool = True
    notes: str = Field(default="", max_length=4000)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("/projects", status_code=201, dependencies=[Depends(authorize)])
def create_project(request: ProductionRequest) -> dict[str, Any]:
    project_id = str(uuid.uuid4())
    stamp = now()
    with database() as db:
        db.execute(
            "INSERT INTO assistant_projects VALUES (?, ?, ?, ?, ?)",
            (project_id, stamp, stamp, "awaiting_review", request.model_dump_json()),
        )
    return {"id": project_id, "status": "awaiting_review"}


@router.get("/projects/{project_id}", dependencies=[Depends(authorize)])
def get_project(project_id: str) -> dict[str, Any]:
    with database() as db:
        row = db.execute("SELECT * FROM assistant_projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return {**dict(row), "payload": json.loads(row["payload"])}


@router.post("/projects/{project_id}/approve", dependencies=[Depends(authorize)])
def approve_project(project_id: str) -> dict[str, str]:
    """Approve the production specification only; does not trigger generation."""
    with database() as db:
        result = db.execute(
            "UPDATE assistant_projects SET status='approved_for_generation', updated_at=? "
            "WHERE id=? AND status='awaiting_review'", (now(), project_id)
        )
        if result.rowcount != 1:
            raise HTTPException(409, "Project missing or not awaiting review")
    return {"id": project_id, "status": "approved_for_generation"}


@router.post("/projects/{project_id}/cancel", dependencies=[Depends(authorize)])
def cancel_project(project_id: str) -> dict[str, str]:
    with database() as db:
        result = db.execute(
            "UPDATE assistant_projects SET status='cancelled', updated_at=? "
            "WHERE id=? AND status IN ('awaiting_review','approved_for_generation')",
            (now(), project_id),
        )
        if result.rowcount != 1:
            raise HTTPException(409, "Project missing or cannot be cancelled")
    return {"id": project_id, "status": "cancelled"}
