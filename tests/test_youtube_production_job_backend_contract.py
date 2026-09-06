from __future__ import annotations

import ast
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("ENABLE_SQLITE_DEV", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/codexia-youtube-production-job-contract.sqlite3")


ROOT = Path(__file__).resolve().parents[1]
YOUTUBE_ROUTER = ROOT / "app" / "routers" / "youtube.py"


class YouTubeProductionJobBackendContractTests(unittest.TestCase):
    def _router_source(self) -> str:
        return YOUTUBE_ROUTER.read_text(encoding="utf-8")

    def test_video_request_declares_fields_that_pydantic_used_to_drop(self):
        tree = ast.parse(self._router_source())
        video_request = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "VideoRequest"
        )
        declared = {
            node.target.id
            for node in video_request.body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        }
        self.assertTrue({
            "production_job_id",
            "approved_narration_required",
            "approved_narration_preview_id",
            "approved_narration_text_sha256",
            "narration_core_version",
            "narration_core_namespace",
        }.issubset(declared))

    def test_api_and_worker_validate_job_before_dedupe_or_image_provider(self):
        tree = ast.parse(self._router_source())
        functions = {
            node.name: ast.get_source_segment(self._router_source(), node) or ""
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        api = functions["generate_video"]
        worker = functions["process_video_generation"]
        self.assertLess(
            api.index("_load_approved_narration_contract("),
            api.index("_build_video_generation_identity("),
        )
        self.assertLess(
            worker.index("_load_approved_narration_contract("),
            worker.index("ensure_image_provider_ready("),
        )
        self.assertIn('script["seed_audio_path"] = approved_narration_contract["render_audio_path"]', worker)
        self.assertIn('script["approved_narration_required"] = True', worker)
        self.assertIn('script["allow_tts_generation"] = False', worker)

    def test_backend_reloads_canonical_job_instead_of_trusting_browser_path(self):
        from app.routers.youtube import VideoRequest, _load_approved_narration_contract
        from app.services.narration_core import NARRATION_CORE_NAMESPACE, NARRATION_CORE_VERSION

        spoken_text = "Jesus é o motivo de eu existir."
        text_sha256 = hashlib.sha256(spoken_text.encode("utf-8")).hexdigest()
        preview_id = "a" * 32
        job_id = "YT-20260906-contract"
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = Path(tmp) / "approved_narration.mp3"
            audio_path.write_bytes(b"ID3" + b"approved" * 256)
            request = VideoRequest(
                mode="story",
                story_content=spoken_text,
                production_job_id=job_id,
                approved_narration_required=True,
                approved_narration_preview_id=preview_id,
                approved_narration_text_sha256=text_sha256,
                narration_core_version=NARRATION_CORE_VERSION,
                narration_core_namespace=NARRATION_CORE_NAMESPACE,
                reuse_audio_from={
                    "production_job_id": job_id,
                    "output_path": "/tmp/path-controlled-by-browser.mp3",
                },
            )
            dumped = request.model_dump()
            self.assertEqual(dumped["production_job_id"], job_id)
            self.assertTrue(dumped["approved_narration_required"])
            stored = {
                "job": {
                    "job_id": job_id,
                    "user_id": 7,
                    "approved_preview_id": preview_id,
                    "approved_audio_sha256": "b" * 64,
                    "approved_audio_meta_path": str(Path(tmp) / "approved_narration.json"),
                },
                "audio_path": audio_path,
                "meta": {
                    "preview_id": preview_id,
                    "approved": True,
                    "text_sha256": text_sha256,
                    "spoken_text_sent_to_tts": spoken_text,
                    "narration_core_version": NARRATION_CORE_VERSION,
                    "narration_core_namespace": NARRATION_CORE_NAMESPACE,
                    "provider": "edge_tts",
                    "voice": "pt-BR-FranciscaNeural",
                },
            }
            with patch(
                "app.services.production_job_store.production_job_store.validated_approved_audio",
                return_value=stored,
            ) as validate:
                contract = _load_approved_narration_contract(request, user_id=7)

        validate.assert_called_once_with(user_id=7, job_id=job_id)
        self.assertEqual(contract["production_job_id"], job_id)
        self.assertEqual(contract["reuse_audio_from"]["output_path"], str(audio_path.resolve()))
        self.assertNotEqual(
            contract["reuse_audio_from"]["output_path"],
            request.reuse_audio_from["output_path"],
        )

    def test_required_contract_fails_closed_without_job_id(self):
        from app.routers.youtube import (
            ApprovedNarrationJobError,
            VideoRequest,
            _load_approved_narration_contract,
        )

        request = VideoRequest(approved_narration_required=True)
        with self.assertRaises(ApprovedNarrationJobError):
            _load_approved_narration_contract(request, user_id=7)

    def test_worker_preserves_exact_mp3_and_rejects_hash_mismatch(self):
        from app.routers.youtube import (
            ApprovedNarrationJobError,
            _preserve_approved_narration_for_task,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio_path = root / "approved_narration.mp3"
            audio_path.write_bytes(b"ID3" + b"approved-audio" * 256)
            audio_sha256 = hashlib.sha256(audio_path.read_bytes()).hexdigest()
            contract = {
                "audio_path": str(audio_path),
                "audio_sha256": audio_sha256,
            }
            with patch.dict(
                os.environ,
                {"CODEXIA_PRODUCTION_MANIFEST_DIR": str(root / "manifests")},
                clear=False,
            ):
                resolved = _preserve_approved_narration_for_task(
                    contract,
                    task_id="task-approved-contract",
                )
                self.assertTrue(Path(resolved["render_audio_path"]).is_file())
                self.assertTrue(resolved["manifest_persisted"])

                tampered_contract = dict(contract)
                tampered_contract["audio_sha256"] = "0" * 64
                with self.assertRaises(ApprovedNarrationJobError):
                    _preserve_approved_narration_for_task(
                        tampered_contract,
                        task_id="task-tampered-contract",
                    )


if __name__ == "__main__":
    unittest.main()
