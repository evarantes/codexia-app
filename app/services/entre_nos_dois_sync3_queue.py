"""Fila fal.ai para UM teste Sync-3: consentimento explícito e sem retry pago.

Não é chamado automaticamente por nenhuma rota. Persistência de reserva
precisa residir em volume durável e compartilhado entre workers; o arquivo
exclusivo previne double-submit local. Falhas ambíguas requerem revisão
manual, nunca reenviar.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib import error, request
from uuid import UUID
from typing import Callable

from app.services.entre_nos_dois_fal_sync3 import FAL_MODEL, prepare_fal_sync3_preview

QUEUE_ENDPOINT = f"https://queue.fal.run/{FAL_MODEL}"
QUEUE_STATUS_BASE = "https://queue.fal.run/fal-ai/sync-lipsync"
_ALLOWED = {"IN_QUEUE", "IN_PROGRESS", "COMPLETED"}


class Sync3QueueError(RuntimeError):
    pass


def _json_request(method: str, url: str, api_key: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = request.Request(
        url, method=method, data=data,
        headers={
            "Authorization": f"Key {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (error.HTTPError, error.URLError, TimeoutError, ValueError):
        raise Sync3QueueError(
            "Resposta da fal.ai inconclusiva; NÃO repetir o envio sem conferir o painel do provedor."
        ) from None
    if not isinstance(result, dict):
        raise Sync3QueueError("Resposta inesperada da fila.")
    return result


def _request_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise Sync3QueueError("ID de solicitação inválido.") from None


def _record(path: Path, data: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def submit_first_sync3_scene(
    *,
    timeline: dict,
    reference_keys: dict[str, str],
    asset_urls: dict[int, dict[str, str]],
    approved_max_usd: float,
    api_key: str,
    durable_receipt_dir: str,
    manual_confirmation: bool = False,
    transport: Callable[[str, str, str, dict | None], dict] = _json_request,
) -> dict:
    """Submete só a cena 1, uma única vez e somente por ação humana.

    Antes do POST grava uma reserva; timeouts/conexões incertas ficam
    bloqueados para evitar cobrar novamente. O operador deverá verificar
    a fal.ai e recuperar o request_id manualmente se ocorrer timeout.
    """
    if manual_confirmation is not True:
        raise Sync3QueueError("Falta confirmação humana para envio com cobrança.")
    if not isinstance(api_key, str) or not api_key.strip():
        raise Sync3QueueError("FAL_KEY não configurada.")
    prepared = prepare_fal_sync3_preview(
        timeline=timeline,
        reference_keys=reference_keys,
        asset_urls=asset_urls,
        approved_max_usd=approved_max_usd,
    )
    if prepared["status"] != "ready_for_explicit_submission":
        raise Sync3QueueError("Prévia não aprovada.")
    project_id = timeline["project_id"]
    folder = Path(durable_receipt_dir).resolve() / "projects" / str(project_id) / "entre-nos-dois"
    folder.mkdir(parents=True, exist_ok=True)
    reservation = folder / "sync3-scene-01.json"
    try:
        fd = os.open(str(reservation), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise Sync3QueueError(
            "Cena já reservada ou enviada. Verifique seu histórico antes de qualquer nova cobrança."
        ) from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({
                "scene_index": 1,
                "model": FAL_MODEL,
                "state": "submission_uncertain",
                "request_id": None,
                "estimated_usd": prepared["estimated_usd"],
            }, file)
        result = transport("POST", QUEUE_ENDPOINT, api_key, prepared["request_input"])
        request_id = _request_id(result.get("request_id"))
        receipt = {
            "scene_index": 1,
            "model": FAL_MODEL,
            "state": "submitted",
            "request_id": request_id,
            "estimated_usd": prepared["estimated_usd"],
            "auto_publish": False,
        }
        _record(reservation, receipt)
        return receipt
    except Exception:
        # Conservar reserva, pois pode ter ocorrido cobrança mesmo sem resposta.
        raise Sync3QueueError(
            "Envio incerto ou falhou; reserva mantida. Verifique o painel fal.ai antes de repetir."
        ) from None


def check_sync3_status(
    *, request_id: str, api_key: str,
    transport: Callable[[str, str, str, dict | None], dict] = _json_request,
) -> dict:
    """Consulta não paga. Nunca tenta submeter novamente."""
    if not api_key or not api_key.strip():
        raise Sync3QueueError("FAL_KEY ausente.")
    rid = _request_id(request_id)
    url = f"{QUEUE_STATUS_BASE}/requests/{rid}/status"
    data = transport("GET", url, api_key, None)
    status = data.get("status")
    if status not in _ALLOWED:
        raise Sync3QueueError("Estado de fila desconhecido.")
    return {
        "request_id": rid,
        "status": status,
        "needs_result_fetch": status == "COMPLETED" and not data.get("error"),
        "requires_human_review": True,
        "auto_publish": False,
    }
