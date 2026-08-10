import os
import sys

import requests


def main() -> int:
    base_url = (os.getenv("CODEXIA_BASE_URL") or "http://127.0.0.1:8011").rstrip("/")
    username = os.getenv("CODEXIA_ADMIN_EMAIL") or "admin@codexia.dev"
    password = os.getenv("CODEXIA_ADMIN_PASSWORD") or "admin123"

    token_resp = requests.post(
        f"{base_url}/token",
        data={"username": username, "password": password},
        timeout=20,
    )
    token_resp.raise_for_status()
    token = (token_resp.json() or {}).get("access_token") or ""
    if not token:
        raise RuntimeError("token_missing")

    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{base_url}/settings/provider-status", headers=headers, timeout=20)
    sys.stdout.write(f"STATUS_CODE {resp.status_code}\n")
    resp.raise_for_status()

    payload = resp.json()
    providers = payload.get("providers") if isinstance(payload, dict) else payload
    if isinstance(providers, dict):
        providers = [{"id": k, **(v or {})} if isinstance(v, dict) else {"id": k, "raw": v} for k, v in providers.items()]
    if not isinstance(providers, list):
        sys.stdout.write(f"PAYLOAD_TYPE {type(payload).__name__}\n")
        if isinstance(payload, dict):
            sys.stdout.write(f"PAYLOAD_KEYS {sorted(list(payload.keys()))}\n")
            sys.stdout.write(f"PROVIDERS_TYPE {type(payload.get('providers')).__name__}\n")
        else:
            sys.stdout.write(f"PROVIDERS_TYPE {type(providers).__name__}\n")
        return 2

    configured = [p.get("id") for p in providers if isinstance(p, dict) and p.get("configured")]
    not_configured = [p.get("id") for p in providers if isinstance(p, dict) and not p.get("configured")]
    sys.stdout.write(f"PROVIDERS_CONFIGURED {configured}\n")
    sys.stdout.write(f"PROVIDERS_NOT_CONFIGURED {not_configured}\n")

    def _status(provider_id: str):
        for p in providers:
            if isinstance(p, dict) and p.get("id") == provider_id:
                return p.get("status")
        return None

    sys.stdout.write(f"OPENAI_STATUS {_status('openai')}\n")
    sys.stdout.write(f"GROQ_STATUS {_status('groq')}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
