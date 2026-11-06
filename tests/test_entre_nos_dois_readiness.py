import unittest

from app.services.entre_nos_dois_readiness import summarize_readiness


class StudioReadinessTests(unittest.TestCase):
    def test_configured_keys_do_not_claim_provider_verified(self):
        result = summarize_readiness(elevenlabs_configured=True, fal_configured=True)
        states = {item["key"]: item["status"] for item in result["steps"]}
        self.assertEqual(states["elevenlabs_key"], "configured")
        self.assertEqual(states["fal_key"], "configured")
        self.assertEqual(states["visual_assets"], "missing")
        self.assertFalse(result["live_provider_access_confirmed"])
        self.assertFalse(result["paid_execution_enabled"])
        self.assertFalse(result["auto_publish"])
        self.assertNotIn("value", str(result))

    def test_private_portraits_are_checked_separately(self):
        partial = summarize_readiness(
            elevenlabs_configured=True, fal_configured=True,
            private_storage_configured=True,
            visual_assets={"Helena": True, "Gabriel": False},
        )
        statuses = {item["key"]: item["status"] for item in partial["steps"]}
        self.assertEqual(statuses["helena_image"], "configured")
        self.assertEqual(statuses["gabriel_image"], "missing")
        self.assertFalse(partial["can_prepare_visual_pilot"])
        self.assertEqual(partial["next_action"], "Cadastrar a imagem de Gabriel")
        complete = summarize_readiness(
            elevenlabs_configured=True, fal_configured=True,
            private_storage_configured=True,
            visual_assets={"Helena": True, "Gabriel": True},
        )
        self.assertTrue(complete["can_prepare_visual_pilot"])
        self.assertFalse(complete["can_generate_paid_audio"])
        self.assertFalse(complete["live_provider_access_confirmed"])

    def test_unconfigured_storage_blocks_visual_pilot(self):
        result = summarize_readiness(
            elevenlabs_configured=True, fal_configured=True,
            private_storage_configured=False,
            visual_assets={"Helena": True, "Gabriel": True},
        )
        self.assertFalse(result["can_prepare_visual_pilot"])
        self.assertEqual(result["next_action"], "Configurar armazenamento privado")

    def test_missing_keys_are_visible(self):
        result = summarize_readiness(elevenlabs_configured=False, fal_configured=False)
        states = {item["key"]: item["status"] for item in result["steps"]}
        self.assertEqual(states["elevenlabs_key"], "missing")
        self.assertEqual(states["fal_key"], "missing")
        self.assertEqual(states["actual_audio"], "not_generated")


if __name__ == "__main__":
    unittest.main()
