import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
VUE_PAGES = (
    STATIC / "index.html",
    STATIC / "login.html",
    STATIC / "reset-password.html",
    STATIC / "pages" / "ai-factory" / "index.html",
    STATIC / "pages" / "bible-video-factory" / "index.html",
    STATIC / "pages" / "humor-factory" / "index.html",
)


class StaticBootResilienceTests(unittest.TestCase):
    def test_vue_is_served_locally_on_every_vue_page(self):
        for page in VUE_PAGES:
            with self.subTest(page=page.relative_to(ROOT)):
                html = page.read_text(encoding="utf-8")
                self.assertIn('/static/vendor/vue.global.prod.js', html)
                self.assertNotIn('unpkg.com/vue', html)

    def test_external_visual_assets_do_not_block_html_parser(self):
        for page in VUE_PAGES:
            with self.subTest(page=page.relative_to(ROOT)):
                html = page.read_text(encoding="utf-8")
                self.assertIn('<script src="https://cdn.tailwindcss.com" defer></script>', html)
                self.assertIn('rel="stylesheet" media="print"', html)

    def test_vendored_vue_bundle_and_service_worker_cache_are_present(self):
        vue_bundle = STATIC / "vendor" / "vue.global.prod.js"
        self.assertGreater(vue_bundle.stat().st_size, 100_000)
        self.assertIn('vue v3.5.18', vue_bundle.read_text(encoding="utf-8")[:200].lower())

        service_worker = (STATIC / "sw.js").read_text(encoding="utf-8")
        self.assertIn("const CACHE_NAME = 'codexia-v6';", service_worker)
        self.assertIn("'/static/vendor/vue.global.prod.js'", service_worker)
        self.assertNotIn('unpkg.com/vue', service_worker)


if __name__ == "__main__":
    unittest.main()
