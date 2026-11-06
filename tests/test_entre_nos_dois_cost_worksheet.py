import unittest

from app.services.entre_nos_dois_cost_worksheet import CostWorksheetError, worksheet
from app.services.entre_nos_dois_voice_plan import prepare_speech_manifest


class TtsCostWorksheetTests(unittest.TestCase):
    def test_worksheet_lists_canonical_segments_without_charging(self):
        output = worksheet()
        self.assertEqual(output["total_characters"], prepare_speech_manifest()["estimated_characters"])
        self.assertEqual(len(output["segments"]), 4)
        self.assertFalse(output["generation_enabled"])
        self.assertFalse(output["billable_calls_performed"])
        self.assertFalse(output["auto_publish"])
        self.assertFalse(output["account_rate_verified"])
        self.assertEqual(len(output["manifest_sha256"]), 64)
        self.assertNotIn("estimated_total_usd", output)

    def test_decimal_estimates_are_nonbinding(self):
        output = worksheet(usd_per_1000_characters="0.25")
        self.assertTrue(output["estimate_only"])
        self.assertEqual(
            sum(item["characters"] for item in output["segments"]),
            output["total_characters"],
        )
        self.assertGreater(float(output["estimated_total_usd"]), 0)
        self.assertFalse(output["quote_is_binding"])

    def test_invalid_rates_fail_closed(self):
        for rate in ("", "0", "-1", "NaN", "Infinity", "1e999", "not-a-rate", "1000.01"):
            with self.subTest(rate=rate), self.assertRaises(CostWorksheetError):
                worksheet(usd_per_1000_characters=rate)

    def test_quote_digest_changes_only_if_approved_script_changes(self):
        self.assertEqual(worksheet()["manifest_sha256"], worksheet(usd_per_1000_characters="1")["manifest_sha256"])


if __name__ == "__main__":
    unittest.main()
