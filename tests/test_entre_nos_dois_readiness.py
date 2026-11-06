import unittest

from app.services.entre_nos_dois_readiness import summarize_readiness


class StudioReadinessTests(unittest.TestCase):
    def test_configured_keys_do_not_claim_provider_verified(self):
        result = summarize_readiness(elevenlabs_configured=True, fal_configured=True)
        states = {item["key"]: item["status"] for item in result["steps"]}
        self.assertEqual(states["elevenlabs_key"], "configured")
        self.assertEqual(states["fal_key"], "configured")
        self.assertEqual(states["visual_assets"], "not_verified")
        self.assertFalse(result["live_provider_access_confirmed"])
        self.assertFalse(result["paid_execution_enabled"])
        self.assertFalse(result["auto_publish"])
        self.assertNotIn("value", str(result))

    def test_missing_keys_are_visible(self):
        result = summarize_readiness(elevenlabs_configured=False, fal_configured=False)
        states = {item["key"]: item["status"] for item in result["steps"]}
        self.assertEqual(states["elevenlabs_key"], "missing")
        self.assertEqual(states["fal_key"], "missing")
        self.assertEqual(states["actual_audio"], "not_generated")


if __name__ == "__main__":
    unittest.main()
