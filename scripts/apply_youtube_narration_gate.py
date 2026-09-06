from __future__ import annotations

import argparse
from pathlib import Path

INDEX = Path("app/static/index.html")
JS = Path("app/static/youtube_narration_gate.js")
YOUTUBE_ROUTER = Path("app/routers/youtube.py")
NARRATION_ROUTER = Path("app/routers/narration_lab.py")
NARRATION_SERVICE = Path("app/services/youtube_narration_gate.py")
PRODUCTION_JOB_STORE = Path("app/services/production_job_store.py")
TAG = '<script src="/static/youtube_narration_gate.js"></script>'
MARKER = "</body>"

# CI synchronization marker: the job-folder contract is source-owned and must
# be validated against the service integration plus the physical job store.


def apply() -> bool:
    """Keep hardening idempotent without rewriting canonical narration sources.

    Since Narration Core v1, the JS, service and routers are source-owned. The
    hardening step may only ensure the static script tag exists; changing the
    canonical source during CI would hide drift between API and worker.
    """
    if not INDEX.is_file():
        raise SystemExit("youtube narration core: index missing")
    text = INDEX.read_text(encoding="utf-8")
    if TAG in text:
        return False
    if MARKER not in text:
        raise SystemExit("youtube narration core: </body> marker not found")
    INDEX.write_text(text.replace(MARKER, f"    {TAG}\n{MARKER}", 1), encoding="utf-8")
    return True


def _require(path: Path, markers: list[str], label: str) -> None:
    if not path.is_file():
        raise SystemExit(f"youtube narration core: {label} missing")
    source = path.read_text(encoding="utf-8")
    missing = [item for item in markers if item not in source]
    if missing:
        raise SystemExit(f"youtube narration core: {label} contract missing {missing}")


def check() -> None:
    text = INDEX.read_text(encoding="utf-8")
    if text.count(TAG) != 1:
        raise SystemExit(f"youtube narration core: expected exactly one script tag, found {text.count(TAG)}")

    # Current UI contract: one job folder is the source of truth. Do not check
    # legacy literal error messages from the pre-job/localStorage-only flow.
    _require(
        JS,
        [
            "Gerar primeiro o áudio da narração",
            "Agora gerar o vídeo com este áudio",
            "Aprovar esta narração",
            "Refazer seguindo minha observação",
            "/youtube/narration-lab/production-preview",
            "/youtube/narration-lab/production-jobs/",
            "production_job_id",
            "reuse_audio_from",
            "approved_narration_text_sha256",
            "approvedLaunchArmed",
            "approved_audio_present",
            "tts_locked",
            "MP3 aprovado ausente na pasta do trabalho",
        ],
        "JS job-folder",
    )

    _require(
        NARRATION_ROUTER,
        [
            "production_job_id",
            "production-preview",
            "production-jobs",
        ],
        "narration router job-folder",
    )

    # The narration service is responsible only for creating/approving previews
    # and delegating durable ownership to the ProductionJobStore.
    _require(
        NARRATION_SERVICE,
        [
            "production_job_id",
            "self.job_store.register_preview",
            "self.job_store.approve_preview",
            "self.job_store.validated_approved_audio",
            "reuse_audio_from",
        ],
        "narration service job integration",
    )

    # The physical job store is the single source of truth for the approved MP3.
    _require(
        PRODUCTION_JOB_STORE,
        [
            "job.json",
            "approved_narration.mp3",
            "approved_narration.json",
            "approved_audio_sha256",
            "tts_locked",
            "_sha256_file",
            "validated_approved_audio",
            "A integridade do MP3 aprovado não confere",
        ],
        "production job store",
    )

    # The video endpoint must carry the job id to the persisted task/worker.
    _require(
        YOUTUBE_ROUTER,
        [
            "production_job_id: Optional[str]",
            "approved_narration_required: bool",
            "_load_approved_narration_contract(",
            "production_job_store.validated_approved_audio",
            'payload.update({',
            '"approved_narration_required": True',
            "_preserve_approved_narration_for_task(",
            'source="approved_narration_core_v1"',
            'script["seed_audio_path"] = approved_narration_contract["render_audio_path"]',
            'script["approved_narration_required"] = True',
            'script["allow_tts_generation"] = False',
            '"tts_regeneration_allowed": False',
            '"approved_narration_validation_failed"',
        ],
        "youtube backend job-folder",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.check:
        parser.error("use --apply and/or --check")
    if args.apply:
        print("youtube narration core:", "applied" if apply() else "already applied")
    if args.check:
        check()
        print("youtube narration core: OK")


if __name__ == "__main__":
    main()
