from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
HARDENING = ROOT / "scripts" / "apply_intelligent_cost_optimization.py"
OPTIMIZER = ROOT / "app" / "services" / "intelligent_cost_optimizer.py"


class IntelligentCostOptimizationHardeningTests(unittest.TestCase):
    def test_hardening_requires_explain_then_confirm_contract(self):
        text = HARDENING.read_text(encoding="utf-8")
        required = (
            "CODEXIA_INTELLIGENT_COST_OPTIMIZATION_V1",
            '"/task/{task_id}/retry-plan"',
            "optimization_plan_hash: Optional[str] = Query(None)",
            "validate_optimization_confirmation(optimization_plan, optimization_plan_hash)",
            "optimization_confirmation_required",
            "OTIMIZAÇÃO INTELIGENTE DE CUSTO",
            "Narração completa será preservada",
            "Nenhum texto será cortado",
            "Novas chamadas pagas de imagem: 0",
        )
        for token in required:
            self.assertIn(token, text)

    def test_worker_falls_back_to_confirmed_request_assets(self):
        text = HARDENING.read_text(encoding="utf-8")
        self.assertIn('request_seed_script = getattr(request, "seeded_script", None)', text)
        self.assertIn('request_selected_images = getattr(request, "selected_images", None)', text)
        self.assertIn('request_reuse_audio = getattr(request, "reuse_audio_from", None)', text)
        self.assertIn('payload["force_render_only"] = True', text)
        self.assertIn('payload["selected_images"] = list(optimization_materials.get("valid_images") or [])', text)

    def test_render_policy_is_zero_paid_media_and_full_content(self):
        text = HARDENING.read_text(encoding="utf-8")
        self.assertIn('"paid_image_calls": 0', text)
        self.assertIn('"preserve_full_narration": True', text)
        self.assertIn('"preserve_full_text": True', text)
        self.assertIn("proportional_visual_index", text)
        self.assertIn("total_visual_groups=max(1, len(group_lookup))", text)

    def test_optimizer_hash_binds_content_and_assets(self):
        text = OPTIMIZER.read_text(encoding="utf-8")
        self.assertIn('"script_sha256": _stable_hash(script_obj)', text)
        self.assertIn('"valid_image_paths": images', text)
        self.assertIn('"audio_path": audio', text)
        self.assertIn('"strategy": "ordered_adjacent_visual_reuse_v1"', text)
        self.assertIn("hmac.compare_digest", text)
        self.assertIn('"never_shorten_narration": True', text)
        self.assertIn('"never_remove_script_text": True', text)
        self.assertIn('"never_generate_paid_images": True', text)


if __name__ == "__main__":
    unittest.main()
