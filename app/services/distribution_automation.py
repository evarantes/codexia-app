import os
import json
import re
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


KDP_DEFAULTS = {
    "login_url": "https://kdp.amazon.com/",
    "bookshelf_url": "https://kdp.amazon.com/pt_BR/bookshelf",
    "timeout_ms": "60000",
    "email_selector": "input#ap_email, input[type='email'], input[name='email']",
    "password_selector": "input#ap_password, input[type='password']",
    "submit_selector": "input#signInSubmit, button#signInSubmit, input[type='submit'], button[type='submit']",
    "new_ebook_button_selector": "text=Criar novo livro ou série, text=Create a new title",
    "title_selector": "input[name*='title' i], input[id*='title' i], input[aria-label*='Título' i], input[aria-label*='Title' i]",
    "subtitle_selector": "input[name*='subtitle' i], input[id*='subtitle' i], input[aria-label*='Subtítulo' i], input[aria-label*='Subtitle' i]",
    "author_selector": "input[name*='author' i], input[id*='author' i], input[aria-label*='Autor' i], input[aria-label*='Author' i]",
    "description_selector": "textarea[name*='description' i], textarea[id*='description' i], textarea[aria-label*='descrição' i], textarea[aria-label*='description' i]",
    "keywords_selector": "input[name*='keyword' i], textarea[name*='keyword' i], input[id*='keyword' i], textarea[id*='keyword' i]",
    "book_file_selector": "input[type='file'][name*='manuscript' i], input[type='file'][id*='manuscript' i], input[type='file'][name*='ebook' i], input[type='file'][id*='ebook' i], input[type='file'][accept*='.epub'], input[type='file'][accept*='.pdf']",
    "cover_file_selector": "input[type='file'][name*='cover' i], input[type='file'][id*='cover' i], input[type='file'][accept*='image']",
    "price_selector": "input[name*='price' i], input[id*='price' i], input[aria-label*='Preço' i], input[aria-label*='Price' i]",
    "publish_selector": "button[type='submit'], input[type='submit'], button[aria-label*='publicar' i], button[aria-label*='publish' i]",
}


def _source_value(source: Any, attr_name: str) -> Any:
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(attr_name)
    return getattr(source, attr_name, None)


def _value_from_source_or_env(
    source: Any,
    attr_name: str,
    env_name: str,
    *,
    default: Optional[str] = None,
    required: bool = False,
) -> str:
    raw = _source_value(source, attr_name)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = os.getenv(env_name)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = default
    value = str(raw or "").strip()
    if required and not value:
        raise DistributionAutomationError(f"Configuração ausente: {attr_name} / {env_name}")
    return value


def _build_kdp_config(source: Any = None) -> Dict[str, Any]:
    cfg = {
        "login_url": _value_from_source_or_env(source, "amazon_kdp_login_url", "KDP_LOGIN_URL", default=KDP_DEFAULTS["login_url"]),
        "bookshelf_url": _value_from_source_or_env(source, "amazon_kdp_bookshelf_url", "KDP_BOOKSHELF_URL", default=KDP_DEFAULTS["bookshelf_url"]),
        "email": _value_from_source_or_env(source, "amazon_kdp_email", "KDP_EMAIL", required=True),
        "password": _value_from_source_or_env(source, "amazon_kdp_password", "KDP_PASSWORD", required=True),
        "timeout_ms": int(_value_from_source_or_env(source, "amazon_kdp_timeout_ms", "KDP_TIMEOUT_MS", default=KDP_DEFAULTS["timeout_ms"]) or "60000"),
        "selectors": {
            "email": _value_from_source_or_env(source, "amazon_kdp_email_selector", "KDP_EMAIL_SELECTOR", default=KDP_DEFAULTS["email_selector"], required=True),
            "password": _value_from_source_or_env(source, "amazon_kdp_password_selector", "KDP_PASSWORD_SELECTOR", default=KDP_DEFAULTS["password_selector"], required=True),
            "submit": _value_from_source_or_env(source, "amazon_kdp_submit_selector", "KDP_SUBMIT_SELECTOR", default=KDP_DEFAULTS["submit_selector"], required=True),
            "new_ebook_url": _value_from_source_or_env(source, "amazon_kdp_new_ebook_url", "KDP_NEW_EBOOK_URL"),
            "new_ebook_button": _value_from_source_or_env(source, "amazon_kdp_new_ebook_button_selector", "KDP_NEW_EBOOK_BUTTON_SELECTOR", default=KDP_DEFAULTS["new_ebook_button_selector"]),
            "title": _value_from_source_or_env(source, "amazon_kdp_title_selector", "KDP_TITLE_SELECTOR", default=KDP_DEFAULTS["title_selector"]),
            "subtitle": _value_from_source_or_env(source, "amazon_kdp_subtitle_selector", "KDP_SUBTITLE_SELECTOR", default=KDP_DEFAULTS["subtitle_selector"]),
            "author": _value_from_source_or_env(source, "amazon_kdp_author_selector", "KDP_AUTHOR_SELECTOR", default=KDP_DEFAULTS["author_selector"]),
            "description": _value_from_source_or_env(source, "amazon_kdp_description_selector", "KDP_DESCRIPTION_SELECTOR", default=KDP_DEFAULTS["description_selector"]),
            "keywords": _value_from_source_or_env(source, "amazon_kdp_keywords_selector", "KDP_KEYWORDS_SELECTOR", default=KDP_DEFAULTS["keywords_selector"]),
            "book_file": _value_from_source_or_env(source, "amazon_kdp_book_file_input_selector", "KDP_BOOK_FILE_INPUT_SELECTOR", default=KDP_DEFAULTS["book_file_selector"]),
            "cover_file": _value_from_source_or_env(source, "amazon_kdp_cover_file_input_selector", "KDP_COVER_FILE_INPUT_SELECTOR", default=KDP_DEFAULTS["cover_file_selector"]),
            "price": _value_from_source_or_env(source, "amazon_kdp_price_selector", "KDP_PRICE_SELECTOR", default=KDP_DEFAULTS["price_selector"]),
            "publish": _value_from_source_or_env(source, "amazon_kdp_publish_selector", "KDP_PUBLISH_SELECTOR", default=KDP_DEFAULTS["publish_selector"]),
        },
    }
    return cfg


def _login_kdp(page: Any, cfg: Dict[str, Any], out_dir: Optional[Path] = None):
    page.goto(cfg["login_url"], wait_until="domcontentloaded")
    if out_dir is not None:
        _safe_capture(page, out_dir, "01_login")
    _guard_against_challenge(page)

    email_sel = str(cfg.get("selectors", {}).get("email") or "").strip()
    if _locator_count(page, email_sel) == 0:
        try:
            page.click("text=/entrar|fazer login|sign in|login/i")
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        if out_dir is not None:
            _safe_capture(page, out_dir, "01b_after_signin_click")
        _guard_against_challenge(page)

    if _locator_count(page, email_sel) == 0:
        try:
            page.goto(cfg["bookshelf_url"], wait_until="domcontentloaded")
        except Exception:
            pass
        if out_dir is not None:
            _safe_capture(page, out_dir, "01c_bookshelf_redirect")
        _guard_against_challenge(page)

    if _locator_count(page, email_sel) == 0:
        raise DistributionAutomationError(
            "Não encontrei o formulário de login da Amazon (campo de e-mail). "
            "A Amazon pode ter mudado a tela, bloqueado o acesso (captcha/2FA) ou exigido verificação. "
            f"URL atual: {getattr(page, 'url', '')}"
        )

    _fill_with_autofix(page, cfg, "email", cfg["email"], [
        KDP_DEFAULTS["email_selector"],
        "input#ap_email",
        "input[type='email']",
        "input[name='email']",
        "input[name='username']",
    ])
    _fill_with_autofix(page, cfg, "password", cfg["password"], [
        KDP_DEFAULTS["password_selector"],
        "input#ap_password",
        "input[type='password']",
    ])
    _click_with_autofix(page, cfg, "submit", [
        KDP_DEFAULTS["submit_selector"],
        "input#signInSubmit",
        "button#signInSubmit",
        "button[type='submit']",
        "input[type='submit']",
        "text=/entrar|sign in|continuar|continue/i",
    ])
    try:
        page.wait_for_load_state("networkidle")
    except Exception:
        pass
    if out_dir is not None:
        _safe_capture(page, out_dir, "02_after_login")


def _locator_count(page: Any, selector: str) -> int:
    try:
        return int(page.locator(selector).count())
    except Exception:
        return 0


def _pick_selector(page: Any, selectors: list[str]) -> str:
    for s in selectors:
        ss = str(s or "").strip()
        if not ss:
            continue
        if _locator_count(page, ss) > 0:
            return ss
    return ""


def _guard_against_challenge(page: Any):
    url = ""
    try:
        url = str(getattr(page, "url", "") or "").lower()
    except Exception:
        url = ""
    challenge_url_tokens = [
        "/ap/cvf/",
        "/ap/mfa",
        "/ap/cnep",
        "/errors/validatecaptcha",
        "auth-mfa",
        "transactionapproval",
        "/ap/challenge",
    ]
    if any(token in url for token in challenge_url_tokens):
        raise DistributionAutomationError(
            f"A Amazon bloqueou o login com uma etapa extra de segurança. URL atual: {getattr(page, 'url', '')}"
        )
    selectors = [
        "input[name='cvf_captcha_input']",
        "input#auth-mfa-otpcode",
        "input[name='otpCode']",
        "input[name='code']",
        "input[name='transactionApprovalStatus']",
        "form[action*='validateCaptcha']",
        "form[action*='verify']",
        "img[alt*='captcha' i]",
    ]
    for selector in selectors:
        if _locator_count(page, selector) > 0:
            raise DistributionAutomationError(
                f"A Amazon exibiu uma validação adicional de segurança. URL atual: {getattr(page, 'url', '')}"
            )
    text_tokens = [
        "enter the characters you see",
        "digite os caracteres",
        "approve sign-in",
        "aprove a entrada",
        "insira o código",
        "enter otp",
        "one time password",
        "senha de uso único",
        "código de verificação",
    ]
    try:
        html = (page.content() or "").lower()
    except Exception:
        return
    for token in text_tokens:
        if token in html:
            raise DistributionAutomationError(
                f"A Amazon exibiu uma validação adicional de segurança. URL atual: {getattr(page, 'url', '')}"
            )


def _fill_with_autofix(page: Any, cfg: Dict[str, Any], key: str, value: str, candidates: list[str]):
    cur = str(cfg.get("selectors", {}).get(key) or "").strip()
    chosen = _pick_selector(page, [cur, *candidates])
    if not chosen:
        raise DistributionAutomationError(f"Não foi possível localizar o campo '{key}' na tela da Amazon (selector inválido).")
    try:
        page.fill(chosen, value or "")
    except Exception:
        raise DistributionAutomationError(f"Falha ao preencher o campo '{key}'. A Amazon pode ter mudado a tela.")
    cfg["selectors"][key] = chosen


def _set_files_with_autofix(page: Any, cfg: Dict[str, Any], key: str, filepath: str, candidates: list[str]):
    cur = str(cfg.get("selectors", {}).get(key) or "").strip()
    chosen = _pick_selector(page, [cur, *candidates])
    if not chosen:
        raise DistributionAutomationError(f"Não foi possível localizar o upload '{key}' na tela da Amazon (selector inválido).")
    try:
        page.set_input_files(chosen, filepath)
    except Exception:
        raise DistributionAutomationError(f"Falha ao enviar arquivo em '{key}'. A Amazon pode ter mudado a tela.")
    cfg["selectors"][key] = chosen


def _click_with_autofix(page: Any, cfg: Dict[str, Any], key: str, candidates: list[str]):
    cur = str(cfg.get("selectors", {}).get(key) or "").strip()
    chosen = _pick_selector(page, [cur, *candidates])
    if not chosen:
        raise DistributionAutomationError(f"Não foi possível localizar o botão '{key}' na tela da Amazon (selector inválido).")
    try:
        page.click(chosen)
    except Exception:
        raise DistributionAutomationError(f"Falha ao clicar em '{key}'. A Amazon pode ter mudado a tela.")
    cfg["selectors"][key] = chosen


def test_kdp_connection_via_browser(settings_obj: Any = None, headless: bool = True) -> Dict[str, Any]:
    cfg = _build_kdp_config(settings_obj)
    out_dir = _log_dir(f"kdp_test_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise DistributionAutomationError(f"Playwright não disponível: {e}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(int(cfg["timeout_ms"]))
        try:
            _login_kdp(page, cfg, out_dir)
            page.goto(cfg["bookshelf_url"], wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle")
            except Exception:
                pass
            title = page.title()
            content = (page.content() or "").lower()
            ok = ("biblioteca" in content) or ("bookshelf" in content)
            return {
                "message": "Conexão com Amazon KDP realizada com sucesso." if ok else "Login realizado, mas a página da biblioteca não foi confirmada automaticamente.",
                "page_title": title,
                "bookshelf_url": cfg["bookshelf_url"],
                "logs_url": f"/generated_assets/distribution_logs/{out_dir.name}/",
            }
        except Exception as e:
            try:
                _safe_capture(page, out_dir, "99_error")
            except Exception:
                pass
            raise DistributionAutomationError(
                f"{str(e)} | Logs: /generated_assets/distribution_logs/{out_dir.name}/"
            )
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


def sync_kdp_bookshelf_via_browser(settings_obj: Any = None, headless: bool = True) -> Dict[str, Any]:
    cfg = _build_kdp_config(settings_obj)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise DistributionAutomationError(f"Playwright não disponível: {e}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(int(cfg["timeout_ms"]))
        try:
            _login_kdp(page, cfg)
            page.goto(cfg["bookshelf_url"], wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle")
            except Exception:
                pass
            items = page.evaluate(
                """
                () => {
                  const clean = (v) => (v || '').replace(/\\s+/g, ' ').trim();
                  const moneyRe = /(?:R\\$|\\$|€|£)\\s?[\\d.,]+(?:\\s?[A-Z]{3})?/i;
                  const statusTokens = ['À venda', 'Rascunho', 'Draft', 'Live', 'Published', 'Publicado'];
                  const nodes = Array.from(document.querySelectorAll('div, section, article, li, tr'))
                    .filter((el) => {
                      const text = clean(el.innerText);
                      if (!/ASIN:\\s*[A-Z0-9]{6,}/i.test(text)) return false;
                      const childWithAsin = Array.from(el.querySelectorAll('div, section, article, li, tr'))
                        .some((child) => child !== el && /ASIN:\\s*[A-Z0-9]{6,}/i.test(clean(child.innerText)));
                      return !childWithAsin && text.length < 4000;
                    });
                  const items = [];
                  for (const el of nodes) {
                    const text = clean(el.innerText);
                    const lines = (el.innerText || '').split(/\\n+/).map(clean).filter(Boolean);
                    let title = '';
                    let author = '';
                    let format = '';
                    let listingStatus = '';
                    let priceText = '';
                    for (const line of lines) {
                      if (!title && !/^Por\\b/i.test(line) && !/^ASIN:/i.test(line) && !/Visualizar na Amazon|View on Amazon|KDP Select|Enviado em|Última modificação|Last modified/i.test(line) && !moneyRe.test(line) && !statusTokens.some((s) => line.includes(s))) {
                        title = line;
                        continue;
                      }
                      if (!author && /^Por\\b/i.test(line)) {
                        author = line.replace(/^Por\\.?[:]?\\s*/i, '').trim();
                        continue;
                      }
                      if (!format && /(eBook Kindle|capa comum|Capa dura|Hardcover|Paperback)/i.test(line)) {
                        format = line;
                      }
                      if (!listingStatus && statusTokens.some((s) => line.includes(s))) {
                        listingStatus = line;
                      }
                      if (!priceText && moneyRe.test(line)) {
                        const m = line.match(moneyRe);
                        priceText = m ? m[0] : '';
                      }
                    }
                    const asinMatch = text.match(/ASIN:\\s*([A-Z0-9]{6,})/i);
                    const asin = asinMatch ? asinMatch[1] : '';
                    const productLinkEl = Array.from(el.querySelectorAll('a')).find((a) => /Visualizar na Amazon|View on Amazon/i.test(clean(a.innerText || '')) || /amazon\\./i.test(a.href || ''));
                    items.push({
                      title,
                      author,
                      asin,
                      format,
                      listing_status: listingStatus,
                      price_text: priceText,
                      product_url: productLinkEl ? productLinkEl.href : '',
                      raw_text: text.slice(0, 2000),
                    });
                  }
                  const seen = new Set();
                  return items.filter((item) => {
                    const key = item.asin || `${item.title}::${item.author}`;
                    if (!key || seen.has(key)) return false;
                    seen.add(key);
                    return Boolean(item.title || item.asin);
                  });
                }
                """
            )
            return {
                "status": "ok",
                "count": len(items or []),
                "items": items or [],
            }
        except Exception as e:
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
    settings_obj: Any = None,
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

    cfg = _build_kdp_config(settings_obj)
    selectors = cfg["selectors"]
    for required_key in ["title", "author", "description", "keywords", "book_file", "cover_file", "publish"]:
        if not str(selectors.get(required_key) or "").strip():
            raise DistributionAutomationError(f"Configuração ausente na Amazon KDP: selector '{required_key}'.")
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
        page.set_default_timeout(int(cfg["timeout_ms"]))
        try:
            _login_kdp(page, cfg, out_dir)
            _guard_against_challenge(page)

            if selectors.get("new_ebook_url"):
                page.goto(selectors["new_ebook_url"], wait_until="domcontentloaded")
            elif selectors.get("new_ebook_button"):
                _click_with_autofix(page, cfg, "new_ebook_button", [
                    "text=/criar novo livro|create a new title/i",
                    KDP_DEFAULTS["new_ebook_button_selector"],
                ])
                try:
                    page.wait_for_load_state("domcontentloaded")
                except Exception:
                    pass
            _safe_capture(page, out_dir, "03_new_ebook")
            _guard_against_challenge(page)

            _fill_with_autofix(page, cfg, "title", title or "", [
                KDP_DEFAULTS["title_selector"],
                "xpath=//label[contains(.,'Título') or contains(.,'Title')]/following::input[1]",
            ])
            if (subtitle or "").strip():
                try:
                    _fill_with_autofix(page, cfg, "subtitle", subtitle or "", [
                        KDP_DEFAULTS["subtitle_selector"],
                        "xpath=//label[contains(.,'Subtítulo') or contains(.,'Subtitle')]/following::input[1]",
                    ])
                except Exception:
                    pass
            _fill_with_autofix(page, cfg, "author", author or "", [
                KDP_DEFAULTS["author_selector"],
                "xpath=//label[contains(.,'Autor') or contains(.,'Author')]/following::input[1]",
            ])
            _fill_with_autofix(page, cfg, "description", (description or "").strip(), [
                KDP_DEFAULTS["description_selector"],
                "xpath=//label[contains(.,'Descrição') or contains(.,'Description')]/following::textarea[1]",
            ])
            _fill_with_autofix(page, cfg, "keywords", (keywords or "").strip(), [
                KDP_DEFAULTS["keywords_selector"],
                "xpath=//label[contains(.,'Palavras') or contains(.,'Keywords')]/following::input[1]",
            ])
            _safe_capture(page, out_dir, "04_metadata")

            _set_files_with_autofix(page, cfg, "book_file", book_file_path, [
                KDP_DEFAULTS["book_file_selector"],
                "xpath=//input[@type='file'][contains(@accept,'.pdf') or contains(@accept,'.epub')]",
            ])
            _set_files_with_autofix(page, cfg, "cover_file", cover_file_path, [
                KDP_DEFAULTS["cover_file_selector"],
                "xpath=//input[@type='file'][contains(@accept,'image')]",
            ])
            _safe_capture(page, out_dir, "05_files")

            if (price or "").strip():
                try:
                    _fill_with_autofix(page, cfg, "price", (price or "").strip(), [
                        KDP_DEFAULTS["price_selector"],
                        "xpath=//label[contains(.,'Preço') or contains(.,'Price')]/following::input[1]",
                    ])
                except Exception:
                    pass

            _click_with_autofix(page, cfg, "publish", [
                KDP_DEFAULTS["publish_selector"],
                "text=/publicar|publish/i",
            ])
            try:
                page.wait_for_load_state("networkidle")
            except Exception:
                pass
            _safe_capture(page, out_dir, "06_published")

            result = {
                "status": "published",
                "target": "kdp",
                "completed_at": datetime.utcnow().isoformat(),
                "selectors_autofixed": cfg.get("selectors", {}),
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
