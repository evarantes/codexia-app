import io
import unittest

from PIL import Image
from scripts.entre_nos_dois_staging_http_e2e import pixel_png, exercise


class StagingHttpE2ETests(unittest.TestCase):
    def test_generated_png_is_decodable_and_safe(self):
        data = pixel_png()
        self.assertTrue(data.startswith(b"\x89PNG"))
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (1, 1))

    def test_refuses_to_run_against_remote_or_production_hosts(self):
        with self.assertRaises(ValueError):
            exercise("https://production.example.com", email="x@example.com",
                     initial_password="a", next_password="b")


if __name__ == "__main__":
    unittest.main()
