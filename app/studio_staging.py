"""Minimal Studio-only ASGI application for disposable staging.

Does NOT import the main API, mount the main dashboard, start scheduler/monitor,
queue workers, legacy startup recovery or any generation endpoints.
Uses a standalone PostgreSQL database via APP_ENV=staging.
"""
from __future__ import annotations

import os
from datetime import timedelta

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.routers import entre_nos_dois
from app.routers.auth import create_access_token_for_user, verify_password, get_password_hash, get_current_admin_user


if os.getenv("APP_ENV", "").strip().lower() != "staging":
    raise RuntimeError("Studio isolado só pode iniciar com APP_ENV=staging.")

app = FastAPI(
    title="Entre Nós Dois — homologação isolada",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.include_router(entre_nos_dois.router)


@app.post("/token")
async def staging_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == form_data.username).first()
    if (
        not user or not user.is_active or not user.is_admin
        or not verify_password(form_data.password, user.hashed_password)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciais administrativas inválidas.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {
        "access_token": create_access_token_for_user(user, expires_delta=timedelta(hours=1)),
        "token_type": "bearer",
        "must_change_password": bool(user.must_change_password),
    }



class PasswordChange(BaseModel):
    current_password: str
    new_password: str


@app.post("/staging/change-password")
def staging_change_password(
    payload: PasswordChange,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin_user),
):
    """Same-user password rotation; required for the initial staging account."""
    if not verify_password(payload.current_password, admin.hashed_password):
        raise HTTPException(status_code=400, detail="Senha atual incorreta.")
    if (
        len(payload.new_password) < 16
        or len(payload.new_password) > 72
        or payload.new_password == payload.current_password
    ):
        raise HTTPException(status_code=400, detail="A nova senha deve ser diferente e ter entre 16 e 72 caracteres.")
    admin.hashed_password = get_password_hash(payload.new_password)
    admin.must_change_password = False
    db.add(admin)
    db.commit()
    return {"changed": True, "must_change_password": False}


@app.get("/health")
def health():
    return {"status": "ok", "mode": "entre-nos-dois-staging"}


@app.get("/")
def root():
    return JSONResponse(
        {"studio": "/studio/entre-nos-dois/painel", "mode": "staging"},
        headers={"Cache-Control": "no-store"},
    )
