"""Create the *first* administrator in a disposable Entre Nós Dois staging DB.

Run interactively inside the staging API container after migrations.
Cannot run outside APP_ENV=staging; never updates existing accounts.
Does not print passwords, tokens or credential hashes.
"""
from __future__ import annotations

import getpass
import os


class StagingBootstrapError(RuntimeError):
    pass


def ensure_staging_environment() -> None:
    if os.getenv("APP_ENV", "").strip().lower() != "staging":
        raise StagingBootstrapError("Bloqueado: exige APP_ENV=staging.")
    url = os.getenv("DATABASE_URL", "").lower()
    if "codexia_staging" not in url or "studio-db" not in url:
        raise StagingBootstrapError("Bloqueado: banco de homologação não confirmado.")


def bootstrap_admin(email: str, password: str, *, session_factory=None) -> dict:
    ensure_staging_environment()
    email = (email or "").strip().lower()
    if not email or "@" not in email or any(c.isspace() for c in email):
        raise StagingBootstrapError("Informe um e-mail válido.")
    if not isinstance(password, str) or len(password) < 16 or len(password) > 72:
        raise StagingBootstrapError("A senha deve ter entre 16 e 72 caracteres.")
    from app.models import User
    from app.routers.auth import get_password_hash
    if session_factory is None:
        from app.database import SessionLocal
        session_factory = SessionLocal
    db = session_factory()
    try:
        # Fail closed on any pre-existing user. This is an empty-staging-only tool.
        if db.query(User).first() is not None:
            raise StagingBootstrapError(
                "Já existem usuários neste banco. Cadastro automático bloqueado."
            )
        user = User(
            email=email,
            name="Admin Entre Nós Dois (homologação)",
            hashed_password=get_password_hash(password),
            is_admin=True,
            role="admin",
            is_active=True,
            must_change_password=True,
        )
        db.add(user)
        db.commit()
        return {"created": True, "environment": "staging", "admin_email": email}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> int:
    ensure_staging_environment()
    email = input("E-mail do administrador exclusivo da homologação: ").strip()
    password = getpass.getpass("Senha nova (16–72 caracteres, não será exibida): ")
    confirm = getpass.getpass("Confirme a senha: ")
    if password != confirm:
        raise StagingBootstrapError("As senhas não coincidem.")
    result = bootstrap_admin(email, password)
    print("Administrador de homologação criado. Troque a senha no primeiro acesso.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StagingBootstrapError as exc:
        print(f"Inicialização bloqueada: {exc}")
        raise SystemExit(1)
