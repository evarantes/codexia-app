from __future__ import annotations

import unittest
from pathlib import Path

from scripts.apply_openai_quality_cost_hardening import patched_text as apply_openai_quality_hardening
from scripts.apply_video_cost_backend_hardening import PatchError, patch_ai_router


ROOT = Path(__file__).resolve().parents[1]


class VideoCostBackendHardeningTests(unittest.TestCase):
    def _openai_router_source(self) -> str:
        source = (ROOT / "app/services/ai_router.py").read_text(encoding="utf-8")
        image_call = source.split("def _call_openai_image(", 1)[1].split("\n    def ", 1)[0]
        if 'quality = str(os.getenv("OPENAI_IMAGE_QUALITY") or "low").strip().lower()' in image_call:
            source = apply_openai_quality_hardening(source)
        return source

    def test_task_quality_is_bound_before_cache_and_openai_call(self):
        transformed = patch_ai_router(self._openai_router_source())
        image_generation = transformed.split("    def generate_image(", 1)[1]

        assignment = image_generation.index(
            "production_mode, production_quality, production_unit_cost = _task_openai_image_profile(task_id)"
        )
        cache_key = image_generation.index(
            'input_hash = _sha256_text(json.dumps({"prompt": raw_prompt, "quality": production_quality, "mode": production_mode}, ensure_ascii=False, sort_keys=True))'
        )
        openai_call = image_generation.index(
            "image_bytes = self._call_openai_image(api_key=api_key, model=model_id, prompt=raw_prompt, quality=production_quality)"
        )

        self.assertLess(assignment, cache_key)
        self.assertLess(cache_key, openai_call)
        self.assertEqual(patch_ai_router(transformed), transformed)

    def test_missing_profile_anchor_fails_build_instead_of_shipping_undefined_name(self):
        source = self._openai_router_source()
        provider_guard = (
            '            if str(policy.primary_provider or "").strip().lower() != "openai":\n'
            '                raise AIOperationBlocked("IMAGE_PROVIDER_NOT_CONFIGURED", "Provider de imagem não configurado.", provider="openai")\n'
            '            if not self._openai_capability_allowed(settings, capability):'
        )
        profile_binding = (
            "            production_mode, production_quality, production_unit_cost = _task_openai_image_profile(task_id)\n"
            "            policy = replace(policy, estimated_cost=production_unit_cost)\n"
        )
        source = source.replace(profile_binding, "", 1)
        self.assertIn(provider_guard, source)
        source = source.replace(
            provider_guard,
            '            if not self._openai_capability_allowed(settings, capability):',
            1,
        )

        with self.assertRaises(PatchError):
            patch_ai_router(source)


if __name__ == "__main__":
    unittest.main()
