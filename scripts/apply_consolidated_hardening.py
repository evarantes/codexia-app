#!/usr/bin/env python3
"""Aplica hardening determinístico em arquivos legados muito grandes.

Este script existe para manter as mudanças críticas auditáveis e idempotentes
sem reescrever milhares de linhas não relacionadas. Ele roda no build e no CI.
Falha imediatamente se a estrutura esperada do código mudar.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]


class PatchError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: esperado 1 trecho original, encontrado {count}")
    return text.replace(old, new, 1)


def _replace_all_expected(text: str, old: str, new: str, *, label: str, min_count: int = 1) -> str:
    if new in text and old not in text:
        return text
    count = text.count(old)
    if count < min_count:
        raise PatchError(f"{label}: trecho original não encontrado")
    return text.replace(old, new)


def patch_factory_page(text: str) -> str:
    """Garante estilo crítico local antes de qualquer CDN nas fábricas legadas.

    O Tailwind Play CDN pode continuar como melhoria progressiva para preservar
    toda a aparência histórica, mas nunca mais é a única fonte de layout. O
    stylesheet local é bloqueante e servido pelo próprio Codexia, portanto uma
    falha/latência externa não pode revelar HTML cru.
    """
    return _replace_once(
        text,
        '<script src="https://cdn.tailwindcss.com" defer></script>',
        '<link href="/static/factory.css" rel="stylesheet">\n'
        '    <script src="https://cdn.tailwindcss.com" defer></script>',
        label="factory/local-critical-css",
    )


def patch_index(text: str) -> str:
    text = _replace_once(
        text,
        '<script src="https://cdn.tailwindcss.com" defer></script>',
        '<script src="https://cdn.tailwindcss.com" onload="document.documentElement.classList.add(\'tailwind-ready\')" onerror="document.documentElement.classList.add(\'tailwind-load-error\')"></script>',
        label="index/tailwind-sync",
    )
    text = _replace_once(
        text,
        '        [v-cloak] { display: none !important; }\n',
        '        [v-cloak] { display: none !important; }\n'
        '        /* Nunca revele o painel sem o Tailwind: evita a tela crua ao abrir/F5. */\n'
        '        html:not(.tailwind-ready) #app { visibility: hidden !important; }\n'
        '        html:not(.tailwind-ready) #loading-overlay { display: flex !important; }\n'
        '        html.tailwind-load-error #app { display: none !important; }\n',
        label="index/tailwind-gate",
    )
    text = _replace_once(
        text,
        "                if (loader) loader.style.display = 'none';",
        "                if (loader && document.documentElement.classList.contains('tailwind-ready')) loader.style.display = 'none';",
        label="index/loader-gate",
    )
    text = _replace_once(
        text,
        '<p class="text-xs mt-1 opacity-90">Google Cloud Console: cadastre o redirect URI exato <code class="font-mono bg-yellow-200 px-1 rounded">http://127.0.0.1:8010/youtube/auth/callback</code>.</p>',
        '<p class="text-xs mt-1 opacity-90">Google Cloud Console: use um cliente <strong>Aplicativo da Web</strong> e cadastre exatamente o redirect URI HTTPS informado pelo Codexia em <code class="font-mono bg-yellow-200 px-1 rounded">/youtube/auth_url</code>.</p>',
        label="index/remove-localhost-help",
    )
    text = _replace_once(
        text,
        "                    if (oauthStatus && (oauthStatus === 'success' || oauthStatus === 'fail' || oauthStatus === 'error')) {\n                        let flashType = oauthStatus;",
        "                    if (oauthStatus && (oauthStatus === 'success' || oauthStatus === 'fail' || oauthStatus === 'error')) {\n                        this.currentTab = 'youtube';\n                        this.youtubeSubTab = 'production';\n                        let flashType = oauthStatus;",
        label="index/oauth-return-tab",
    )
    return text


def patch_youtube_router(text: str) -> str:
    text = _replace_once(
        text,
        'from app.services.youtube_service import YouTubeService\n',
        'from app.services.youtube_service import YouTubeService\n'
        'from app.services.youtube_channel_context import load_channel_context_text, load_channel_snapshot, save_channel_snapshot\n'
        'from app.services.youtube_publication_reconciler import reconcile_pending_youtube_publications\n',
        label="youtube/import-decoupling-services",
    )
    text = _replace_once(
        text,
        '        "auto_upload": bool(payload.get("auto_upload")),\n',
        '        # auto_upload intencionalmente fora da identidade: publicar não cria outro MP4.\n',
        label="youtube/production-identity-independent-from-publish",
    )
    text = _replace_once(
        text,
        '@router.get("/stats")\ndef get_stats():\n    service = YouTubeService()\n    return service.get_channel_stats()\n',
        '@router.get("/stats")\ndef get_stats(\n'
        '    db: Session = Depends(get_db),\n'
        '    current_user: Optional[User] = Depends(get_current_admin_user),\n'
        '):\n'
        '    """Canal ao vivo quando conectado; último snapshot quando estiver offline.\n\n'
        '    A disponibilidade do YouTube não bloqueia a fábrica. Ao reconectar,\n'
        '    esta leitura também tenta escoar publicações já autorizadas.\n'
        '    """\n'
        '    service = YouTubeService()\n'
        '    live_raw = service.get_channel_stats()\n'
        '    live = dict(live_raw) if isinstance(live_raw, dict) else {}\n'
        '    user_id = int(current_user.id) if current_user is not None and getattr(current_user, "id", None) else None\n'
        '    connected = bool(getattr(service, "service", None)) and not bool(live.get("error"))\n'
        '    if connected:\n'
        '        live["connected"] = True\n'
        '        try:\n'
        '            snapshot = save_channel_snapshot(db, user_id=user_id, stats=live)\n'
        '            live["channel_snapshot_cached"] = True\n'
        '            live["channel_snapshot_cached_at"] = snapshot.get("cached_at")\n'
        '        except Exception:\n'
        '            live["channel_snapshot_cached"] = False\n'
        '        if user_id is not None:\n'
        '            try:\n'
        '                live["publication_reconcile"] = reconcile_pending_youtube_publications(\n'
        '                    db, user_id=user_id, limit=3, service=service\n'
        '                )\n'
        '            except Exception as exc:\n'
        '                live["publication_reconcile"] = {\n'
        '                    "connected": True, "attempted": 0, "published": 0,\n'
        '                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",\n'
        '                }\n'
        '        return live\n'
        '    try:\n'
        '        cached = load_channel_snapshot(db, user_id=user_id) or {}\n'
        '    except Exception:\n'
        '        cached = {}\n'
        '    response = {**cached, **live}\n'
        '    response["connected"] = False\n'
        '    response["using_cached_channel_profile"] = bool(cached)\n'
        '    response["cached_channel_profile"] = cached or None\n'
        '    response.setdefault("error", getattr(service, "auth_error", None) or "YouTube desconectado")\n'
        '    return response\n',
        label="youtube/stats-live-cache-reconcile",
    )
    text = _replace_once(
        text,
        '    identity = _build_video_generation_identity(payload)\n    payload["idempotency_key"] = identity["idempotency_key"]\n',
        '    # A intenção de publicar não participa da identidade da produção.\n'
        '    # Assim, conectar/desconectar o YouTube nunca cria uma nova renderização.\n'
        '    identity = _build_video_generation_identity(payload)\n'
        '    try:\n'
        '        cached_channel_context = load_channel_context_text(\n'
        '            db,\n'
        '            user_id=(int(current_user.id) if current_user is not None and getattr(current_user, "id", None) else None),\n'
        '        )\n'
        '    except Exception:\n'
        '        cached_channel_context = ""\n'
        '    if cached_channel_context:\n'
        '        payload["channel_context"] = cached_channel_context\n'
        '        original_topic = str(payload.get("topic") or "").strip()\n'
        '        if original_topic:\n'
        '            payload["topic"] = f"{original_topic}\\n\\n{cached_channel_context}"\n'
        '    payload["idempotency_key"] = identity["idempotency_key"]\n',
        label="youtube/use-cached-channel-context",
    )
    text = _replace_once(
        text,
        '    redirect_base = "/youtube-auto?tab=settings"',
        '    redirect_base = "/"',
        label="youtube/oauth-return-root",
    )
    text = _replace_once(
        text,
        '                   "Google Cloud: tipo Desktop ou Web com redirect_uri "\n                   "http://127.0.0.1:8010/youtube/auth/callback",',
        '                   "Google Cloud: use um cliente Web e cadastre exatamente o redirect_uri "\n                   "HTTPS retornado por /youtube/auth_url.",',
        label="youtube/remove-localhost-help",
    )
    return text


def patch_unified_pipeline(text: str) -> str:
    text = _replace_once(
        text,
        '    "callback_url",\n}',
        '    "callback_url",\n    "auto_publish",  # publicação não muda a identidade do MP4\n}',
        label="pipeline/ignore-publish-in-production-hash",
    )
    text = _replace_once(
        text,
        '    merged.setdefault("auto_upload", bool(req.auto_publish))',
        '    # Produção e publicação são fases independentes: o worker de render nunca faz upload.\n'
        '    merged.setdefault("auto_upload", False)\n'
        '    merged.setdefault("publish_requested", bool(req.auto_publish))',
        label="pipeline/no-upload-during-render",
    )
    text = _replace_once(
        text,
        '            self.transition_status(\n'
        '                db,\n'
        '                str(uv.task_id or uv.idempotency_key),\n'
        '                status=UnifiedVideoStatus.FAILED,\n'
        '                message=f"Upload falhou: {type(exc).__name__}: {str(exc)[:300]}",\n'
        '                merge_result={"publish_error": {"type": type(exc).__name__, "message": str(exc)[:300]}},\n'
        '            )\n'
        '            return {"ok": False, "code": "exception", "error": f"{type(exc).__name__}: {str(exc)[:300]}", "youtube_video_id": None}',
        '            pending_status = (\n'
        '                UnifiedVideoStatus.AWAITING_REVIEW\n'
        '                if bool(uv.review_required)\n'
        '                else UnifiedVideoStatus.APPROVED\n'
        '            )\n'
        '            self.transition_status(\n'
        '                db,\n'
        '                str(uv.task_id or uv.idempotency_key),\n'
        '                status=pending_status,\n'
        '                progress=100,\n'
        '                message="Vídeo pronto; publicação no YouTube pendente. A produção foi preservada.",\n'
        '                merge_result={\n'
        '                    "publish_pending": True,\n'
        '                    "production_preserved": True,\n'
        '                    "publish_error": {"type": type(exc).__name__, "message": str(exc)[:300]},\n'
        '                },\n'
        '            )\n'
        '            return {\n'
        '                "ok": False,\n'
        '                "code": "publication_pending",\n'
        '                "production_preserved": True,\n'
        '                "error": f"{type(exc).__name__}: {str(exc)[:300]}",\n'
        '                "youtube_video_id": None,\n'
        '            }',
        label="pipeline/publication-failure-preserves-production",
    )
    return text


PATCHERS: dict[str, Callable[[str], str]] = {
    "app/static/index.html": patch_index,
    "app/static/pages/ai-factory/index.html": patch_factory_page,
    "app/static/pages/bible-video-factory/index.html": patch_factory_page,
    "app/static/pages/humor-factory/index.html": patch_factory_page,
    "app/routers/youtube.py": patch_youtube_router,
    "app/services/unified_video_pipeline.py": patch_unified_pipeline,
}


def apply(*, write: bool) -> int:
    changed = 0
    for rel_path, patcher in PATCHERS.items():
        path = ROOT / rel_path
        original = path.read_text(encoding="utf-8")
        transformed = patcher(original)
        if transformed != original:
            changed += 1
            if write:
                path.write_text(transformed, encoding="utf-8")
        # Idempotência obrigatória: uma segunda passagem não pode mudar nada.
        second = patcher(transformed)
        if second != transformed:
            raise PatchError(f"{rel_path}: transformação não é idempotente")
    mode = "aplicados" if write else "necessários"
    print(f"Hardening consolidado: {changed} arquivo(s) {mode}; contratos validados={len(PATCHERS)}")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="grava as transformações")
    parser.add_argument("--check", action="store_true", help="somente valida que os padrões ainda são reconhecidos")
    args = parser.parse_args()
    write = bool(args.apply)
    if not args.apply and not args.check:
        parser.error("use --apply ou --check")
    try:
        apply(write=write)
    except PatchError as exc:
        print(f"ERRO DE HARDENING: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
