import os
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


class DistributionAutomationError(Exception):
    pass


def _get_env(name: str, required: bool = True) -> str:
    v = (os.getenv(name) or "").strip()
    if required and not v:
        raise DistributionAutomationError(f"Env var ausente: {name}")
    return v


def _log_dir(task_id: str) -> Path:
    base = Path("generated_assets") / "distribution_logs" / (task_id or "unknown")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _write_json(path: Path, data: Dict[str, Any]):
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        try:
            path.write_text(str(data), encoding="utf-8")
        except Exception:
            pass


def _safe_capture(page: Any, out_dir: Path, name: str):
    try:
        page.screenshot(path=str(out_dir / f"{name}.png"), full_page=True)
    except Exception:
        pass
    try:
        html = page.content()
        (out_dir / f"{name}.html").write_text(html, encoding="utf-8")
    except Exception:
        pass


def _provider_prefix(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p in {"onerpm", "one_rpm", "one-rpm"}:
        return "ONERPM"
    if p in {"offstep", "off_step", "off-step"}:
        return "OFFSTEP"
    raise DistributionAutomationError("Provider inválido. Use: onerpm ou offstep.")


def distribute_music_via_browser(
    task_id: str,
    provider: str,
    audio_path: str,
    cover_path: str,
    title: str,
    artist: str,
    genre: str,
    release_date: str,
    lyrics: Optional[str] = None,
    headless: bool = True,
) -> Dict[str, Any]:
    out_dir = _log_dir(task_id)
    _write_json(out_dir / "input.json", {
        "provider": provider,
        "audio_path": audio_path,
        "cover_path": cover_path,
        "title": title,
        "artist": artist,
        "genre": genre,
        "release_date": release_date,
        "has_lyrics": bool((lyrics or "").strip()),
        "started_at": datetime.utcnow().isoformat(),
    })

    prefix = _provider_prefix(provider)

    login_url = _get_env(f"{prefix}_LOGIN_URL")
    email = _get_env(f"{prefix}_EMAIL")
    password = _get_env(f"{prefix}_PASSWORD")
    timeout_ms = int((_get_env(f"{prefix}_TIMEOUT_MS", required=False) or "60000").strip() or "60000")

    selectors = {
        "email": _get_env(f"{prefix}_EMAIL_SELECTOR"),
        "password": _get_env(f"{prefix}_PASSWORD_SELECTOR"),
        "submit": _get_env(f"{prefix}_SUBMIT_SELECTOR"),
        "new_release_url": _get_env(f"{prefix}_NEW_RELEASE_URL", required=False),
        "new_release_button": _get_env(f"{prefix}_NEW_RELEASE_BUTTON_SELECTOR", required=False),
        "audio_input": _get_env(f"{prefix}_AUDIO_INPUT_SELECTOR"),
        "cover_input": _get_env(f"{prefix}_COVER_INPUT_SELECTOR"),
        "title": _get_env(f"{prefix}_TITLE_SELECTOR"),
        "artist": _get_env(f"{prefix}_ARTIST_SELECTOR"),
        "genre": _get_env(f"{prefix}_GENRE_SELECTOR"),
        "release_date": _get_env(f"{prefix}_RELEASE_DATE_SELECTOR"),
        "lyrics": _get_env(f"{prefix}_LYRICS_SELECTOR", required=False),
        "submit_release": _get_env(f"{prefix}_SUBMIT_RELEASE_SELECTOR"),
    }
    _write_json(out_dir / "selectors.json", selectors)

    if not os.path.isfile(audio_path):
        raise DistributionAutomationError("Arquivo de áudio não encontrado no servidor.")
    if not os.path.isfile(cover_path):
        raise DistributionAutomationError("Arquivo de capa não encontrado no servidor.")

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise DistributionAutomationError(f"Playwright não disponível: {e}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        try:
            page.goto(login_url, wait_until="domcontentloaded")
            _safe_capture(page, out_dir, "01_login")
            page.fill(selectors["email"], email)
            page.fill(selectors["password"], password)
            page.click(selectors["submit"])
            try:
                page.wait_for_load_state("networkidle")
            except Exception:
                pass
            _safe_capture(page, out_dir, "02_after_login")

            if selectors["new_release_url"]:
                page.goto(selectors["new_release_url"], wait_until="domcontentloaded")
            elif selectors["new_release_button"]:
                page.click(selectors["new_release_button"])
                try:
                    page.wait_for_load_state("domcontentloaded")
                except Exception:
                    pass
            _safe_capture(page, out_dir, "03_new_release")

            page.set_input_files(selectors["audio_input"], audio_path)
            page.set_input_files(selectors["cover_input"], cover_path)
            _safe_capture(page, out_dir, "04_files")

            if title:
                page.fill(selectors["title"], title)
            if artist:
                page.fill(selectors["artist"], artist)
            if genre:
                page.fill(selectors["genre"], genre)
            if release_date:
                page.fill(selectors["release_date"], release_date)
            if selectors.get("lyrics") and (lyrics or "").strip():
                try:
                    page.fill(selectors["lyrics"], (lyrics or "").strip())
                except Exception:
                    pass
            _safe_capture(page, out_dir, "05_metadata")

            page.click(selectors["submit_release"])
            try:
                page.wait_for_load_state("networkidle")
            except Exception:
                pass
            _safe_capture(page, out_dir, "06_submitted")

            result = {
                "status": "submitted",
                "provider": provider,
                "completed_at": datetime.utcnow().isoformat(),
            }
            _write_json(out_dir / "result.json", result)
            return result
        except Exception as e:
            _safe_capture(page, out_dir, "99_error")
            err = {"status": "failed", "error": str(e), "provider": provider, "failed_at": datetime.utcnow().isoformat()}
            _write_json(out_dir / "result.json", err)
            raise DistributionAutomationError(str(e))
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


def publish_ebook_kdp_via_browser(
    task_id: str,
    book_file_path: str,
    cover_file_path: str,
    title: str,
    subtitle: str,
    author: str,
    description: str,
    keywords: str,
    price: Optional[str] = None,
    headless: bool = True,
) -> Dict[str, Any]:
    out_dir = _log_dir(task_id)
    _write_json(out_dir / "input.json", {
        "target": "kdp",
        "book_file_path": book_file_path,
        "cover_file_path": cover_file_path,
        "title": title,
        "subtitle": subtitle,
        "author": author,
        "has_description": bool((description or "").strip()),
        "has_keywords": bool((keywords or "").strip()),
        "price": price,
        "started_at": datetime.utcnow().isoformat(),
    })

    login_url = _get_env("KDP_LOGIN_URL")
    email = _get_env("KDP_EMAIL")
    password = _get_env("KDP_PASSWORD")
    timeout_ms = int((_get_env("KDP_TIMEOUT_MS", required=False) or "60000").strip() or "60000")

    selectors = {
        "email": _get_env("KDP_EMAIL_SELECTOR"),
        "password": _get_env("KDP_PASSWORD_SELECTOR"),
        "submit": _get_env("KDP_SUBMIT_SELECTOR"),
        "new_ebook_url": _get_env("KDP_NEW_EBOOK_URL", required=False),
        "new_ebook_button": _get_env("KDP_NEW_EBOOK_BUTTON_SELECTOR", required=False),
        "title": _get_env("KDP_TITLE_SELECTOR"),
        "subtitle": _get_env("KDP_SUBTITLE_SELECTOR", required=False),
        "author": _get_env("KDP_AUTHOR_SELECTOR"),
        "description": _get_env("KDP_DESCRIPTION_SELECTOR"),
        "keywords": _get_env("KDP_KEYWORDS_SELECTOR"),
        "book_file": _get_env("KDP_BOOK_FILE_INPUT_SELECTOR"),
        "cover_file": _get_env("KDP_COVER_FILE_INPUT_SELECTOR"),
        "price": _get_env("KDP_PRICE_SELECTOR", required=False),
        "publish": _get_env("KDP_PUBLISH_SELECTOR"),
    }
    _write_json(out_dir / "selectors.json", selectors)

    if not os.path.isfile(book_file_path):
        raise DistributionAutomationError("Arquivo do livro não encontrado no servidor.")
    if not os.path.isfile(cover_file_path):
        raise DistributionAutomationError("Arquivo de capa do livro não encontrado no servidor.")

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise DistributionAutomationError(f"Playwright não disponível: {e}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        try:
            page.goto(login_url, wait_until="domcontentloaded")
            _safe_capture(page, out_dir, "01_login")
            page.fill(selectors["email"], email)
            page.fill(selectors["password"], password)
            page.click(selectors["submit"])
            try:
                page.wait_for_load_state("networkidle")
            except Exception:
                pass
            _safe_capture(page, out_dir, "02_after_login")

            if selectors["new_ebook_url"]:
                page.goto(selectors["new_ebook_url"], wait_until="domcontentloaded")
            elif selectors["new_ebook_button"]:
                page.click(selectors["new_ebook_button"])
                try:
                    page.wait_for_load_state("domcontentloaded")
                except Exception:
                    pass
            _safe_capture(page, out_dir, "03_new_ebook")

            page.fill(selectors["title"], title or "")
            if selectors.get("subtitle") and subtitle:
                try:
                    page.fill(selectors["subtitle"], subtitle)
                except Exception:
                    pass
            page.fill(selectors["author"], author or "")
            page.fill(selectors["description"], (description or "").strip())
            page.fill(selectors["keywords"], (keywords or "").strip())
            _safe_capture(page, out_dir, "04_metadata")

            page.set_input_files(selectors["book_file"], book_file_path)
            page.set_input_files(selectors["cover_file"], cover_file_path)
            _safe_capture(page, out_dir, "05_files")

            if selectors.get("price") and (price or "").strip():
                try:
                    page.fill(selectors["price"], (price or "").strip())
                except Exception:
                    pass

            page.click(selectors["publish"])
            try:
                page.wait_for_load_state("networkidle")
            except Exception:
                pass
            _safe_capture(page, out_dir, "06_published")

            result = {
                "status": "published",
                "target": "kdp",
                "completed_at": datetime.utcnow().isoformat(),
            }
            _write_json(out_dir / "result.json", result)
            return result
        except Exception as e:
            _safe_capture(page, out_dir, "99_error")
            err = {"status": "failed", "error": str(e), "target": "kdp", "failed_at": datetime.utcnow().isoformat()}
            _write_json(out_dir / "result.json", err)
            raise DistributionAutomationError(str(e))
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
