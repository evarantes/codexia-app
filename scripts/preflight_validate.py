import os
import pathlib
import py_compile
import socket
import subprocess
import sys
import time


ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"


def _safe_print(message: str) -> None:
    text = str(message)
    try:
        print(text)
    except UnicodeEncodeError:
        fallback = text.encode("ascii", errors="replace").decode("ascii")
        print(fallback)


def _compile_all_python() -> None:
    py_files = sorted(APP_DIR.rglob("*.py"))
    if not py_files:
        raise RuntimeError("Nenhum arquivo Python encontrado em app/.")
    for path in py_files:
        py_compile.compile(str(path), doraise=True)
    _safe_print(f"[preflight] py_compile OK em {len(py_files)} arquivos.")


def _run_schema_consistency_check(env: dict[str, str], database_url_supplied: bool) -> None:
    if not database_url_supplied:
        _safe_print("[preflight] schema check ignorado: DATABASE_URL real não foi fornecida.")
        return
    cmd = [sys.executable, str(ROOT / "scripts" / "check_schema_consistency.py")]
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if output.strip():
        for line in output.splitlines():
            _safe_print(line)
    if proc.returncode != 0:
        raise RuntimeError("Schema check falhou; revise Alembic/current e as tabelas críticas.")


def _wait_port(host: str, port: int, timeout_sec: float) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _start_uvicorn_and_verify() -> None:
    env = os.environ.copy()
    database_url_supplied = bool((env.get("DATABASE_URL") or "").strip())
    env.setdefault("ADMIN_EMAIL", "admin@codexia.dev")
    env.setdefault("ADMIN_PASSWORD", "admin123")
    env.setdefault("ADMIN_NAME", "Admin Dev")
    env.setdefault("SECRET_KEY", "dev-secret-key-codexia-2025")
    env.setdefault("APP_ENV", "development")
    env.setdefault("DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/codexia")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("USE_STATIC_VIDEOS", "1")
    env.setdefault("USE_STATIC_MUSIC", "1")
    env.setdefault("USE_STATIC_BOOKS", "1")

    host = env.get("PREFLIGHT_HOST", "127.0.0.1")
    port = int(env.get("PREFLIGHT_PORT", "8011"))
    startup_timeout = float(env.get("PREFLIGHT_STARTUP_TIMEOUT_SEC", "25"))

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        host,
        "--port",
        str(port),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    lines = []
    startup_ok = False
    try:
        deadline = time.time() + startup_timeout
        while time.time() < deadline:
            line = proc.stdout.readline() if proc.stdout is not None else ""
            if line:
                lines.append(line.rstrip())
                _safe_print(line.rstrip())
                if "Application startup complete." in line or "Uvicorn running on" in line:
                    startup_ok = True
                    break
                if "ImportError" in line or "ModuleNotFoundError" in line:
                    break
            elif proc.poll() is not None:
                break
            else:
                time.sleep(0.2)

        if not startup_ok and _wait_port(host, port, 3.0):
            startup_ok = True

        if not startup_ok:
            raise RuntimeError(
                "Uvicorn não confirmou startup completo.\n"
                + "\n".join(lines[-40:])
            )
        _safe_print("[preflight] uvicorn iniciou sem ImportError.")
        _run_schema_consistency_check(env, database_url_supplied)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def main() -> int:
    try:
        _compile_all_python()
        _start_uvicorn_and_verify()
        _safe_print("[preflight] validação concluída com sucesso.")
        return 0
    except Exception as exc:
        _safe_print(f"[preflight] falha: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
