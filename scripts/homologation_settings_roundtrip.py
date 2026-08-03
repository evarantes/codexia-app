import os
import sys

import requests


def main() -> int:
    base_url = (os.getenv("CODEXIA_BASE_URL") or "http://127.0.0.1:8011").rstrip("/")
    username = os.getenv("CODEXIA_ADMIN_EMAIL") or "admin@codexia.dev"
    password = os.getenv("CODEXIA_ADMIN_PASSWORD") or "admin123"
    field = os.getenv("CODEXIA_SETTINGS_FIELD") or "openrouter_model"
    temp_value = os.getenv("CODEXIA_SETTINGS_TEMP_VALUE") or "homolog_tmp_model"

    token_resp = requests.post(
        f"{base_url}/token",
        data={"username": username, "password": password},
        timeout=20,
    )
    if token_resp.status_code != 200:
        sys.stdout.write(f"TOKEN_STATUS {token_resp.status_code}\n")
        return 1

    token = (token_resp.json() or {}).get("access_token") or ""
    if not token:
        sys.stdout.write("TOKEN_STATUS 200\n")
        sys.stdout.write("TOKEN_MISSING YES\n")
        return 1

    headers = {"Authorization": f"Bearer {token}"}

    get_resp = requests.get(f"{base_url}/settings/", headers=headers, timeout=20)
    sys.stdout.write(f"GET_STATUS {get_resp.status_code}\n")
    if get_resp.status_code != 200:
        return 1
    current = get_resp.json() or {}
    old_value = current.get(field)

    set_resp = requests.post(
        f"{base_url}/settings/",
        headers=headers,
        json={field: temp_value},
        timeout=20,
    )
    sys.stdout.write(f"SET_STATUS {set_resp.status_code}\n")

    get2 = requests.get(f"{base_url}/settings/", headers=headers, timeout=20)
    read_value = (get2.json() or {}).get(field) if get2.status_code == 200 else None
    sys.stdout.write(f"VERIFY_SET {str(get2.status_code == 200 and read_value == temp_value).upper()}\n")

    restore_resp = requests.post(
        f"{base_url}/settings/",
        headers=headers,
        json={field: old_value},
        timeout=20,
    )
    sys.stdout.write(f"RESTORE_STATUS {restore_resp.status_code}\n")

    get3 = requests.get(f"{base_url}/settings/", headers=headers, timeout=20)
    restored_value = (get3.json() or {}).get(field) if get3.status_code == 200 else None
    sys.stdout.write(f"VERIFY_RESTORE {str(get3.status_code == 200 and restored_value == old_value).upper()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

