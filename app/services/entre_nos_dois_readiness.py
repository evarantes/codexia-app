"""Readiness snapshot without provider calls, generation or secret values."""
from __future__ import annotations


def summarize_readiness(
    *,
    elevenlabs_configured: bool,
    fal_configured: bool,
    private_storage_configured: bool = False,
    visual_assets: dict[str, bool] | None = None,
) -> dict:
    """Separate configured credentials and private assets from actual provider access."""
    visual_assets = visual_assets or {}
    gabriel = visual_assets.get("Gabriel") is True
    helena = visual_assets.get("Helena") is True
    both_ready = gabriel and helena
    steps = [
        {"key": "voice_ids", "label": "Vozes Andrey e Luciana", "status": "configured", "detail": "Identificadores registrados; acesso real ainda não verificado"},
        {"key": "elevenlabs_key", "label": "Credencial ElevenLabs", "status": "configured" if elevenlabs_configured else "missing", "detail": "Presença da chave; não comprova acesso às vozes"},
        {"key": "fal_key", "label": "Credencial fal.ai", "status": "configured" if fal_configured else "missing", "detail": "Presença da chave; saldo e custo não verificados"},
        {"key": "private_storage", "label": "Armazenamento privado", "status": "configured" if private_storage_configured else "missing", "detail": "Google Drive conectado e volume de metadados pronto" if private_storage_configured else "Conecte o Google Drive nas Configurações e configure o volume privado de metadados"},
        {"key": "gabriel_image", "label": "Referência de Gabriel", "status": "configured" if gabriel else "missing", "detail": "Imagem privada cadastrada" if gabriel else "Imagem ainda não cadastrada"},
        {"key": "helena_image", "label": "Referência de Helena", "status": "configured" if helena else "missing", "detail": "Imagem privada cadastrada" if helena else "Imagem ainda não cadastrada"},
        {"key": "visual_assets", "label": "Imagens oficiais privadas", "status": "configured" if both_ready else "missing", "detail": "Ambas as referências encontradas" if both_ready else "Cadastre ambas as imagens"},
        {"key": "actual_audio", "label": "Áudio real", "status": "not_generated", "detail": "Nenhuma fala gerada por este fluxo"},
        {"key": "lip_sync", "label": "Sincronização labial", "status": "not_generated", "detail": "Aguardar voz, áudio, imagens e aprovação de custo"},
    ]
    return {
        "steps": steps,
        "can_check_voices": bool(elevenlabs_configured),
        "can_prepare_visual_pilot": bool(private_storage_configured and both_ready),
        "can_generate_paid_audio": False,
        "next_action": (
            "Conectar o Google Drive e configurar o volume de metadados"
            if not private_storage_configured
            else "Cadastrar a imagem de Helena"
            if not helena else "Cadastrar a imagem de Gabriel"
            if not gabriel else "Configurar credencial ElevenLabs"
            if not elevenlabs_configured else "Verificar acesso real às vozes Andrey e Luciana"
        ),
        "paid_execution_enabled": False,
        "auto_publish": False,
        "live_provider_access_confirmed": False,
    }
