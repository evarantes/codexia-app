import unittest
from unittest.mock import Mock

from app.routers.entre_nos_dois import overview, router


class StudioOverviewTests(unittest.TestCase):
    def test_admin_only_route_is_declared_as_read_only(self):
        routes = [r for r in router.routes if r.path.endswith("/overview")]
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].methods, {"GET"})

    def test_overview_does_not_expose_voice_ids_or_secret_tokens(self):
        data = overview(current_user=Mock(id=42))
        self.assertEqual(data["pilot_title"], "Quem deve pedir desculpas primeiro no casamento?")
        self.assertEqual([h["voice_label"] for h in data["hosts"]], ["Andrey", "Luciana"])
        self.assertEqual(len(data["dialogue"]), 4)
        self.assertFalse(data["auto_publish"])
        self.assertFalse(data["production_ready"])
        self.assertNotIn("ceOf6Sb1xKhC0ZVxHIzb", str(data))
        self.assertNotIn("EMGk4qRFrRTnkdPoTzu3", str(data))
        self.assertNotIn("api_key", str(data).lower())


if __name__ == "__main__":
    unittest.main()
