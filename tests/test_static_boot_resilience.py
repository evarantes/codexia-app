import unittest
import re
from html.parser import HTMLParser
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


class _VueElseAdjacencyParser(HTMLParser):
    _VOID_ELEMENTS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        attr_names = {name for name, _value in attrs}
        siblings = self.stack[-1]["children"] if self.stack else []
        if "v-else" in attr_names or "v-else-if" in attr_names:
            previous_attrs = siblings[-1]["attrs"] if siblings else set()
            if "v-if" not in previous_attrs and "v-else-if" not in previous_attrs:
                self.errors.append(f"<{tag}> com v-else sem v-if adjacente")

        node = {"tag": tag, "attrs": attr_names, "children": []}
        if self.stack:
            self.stack[-1]["children"].append(node)
        if tag not in self._VOID_ELEMENTS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1]["tag"] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                return


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

    def test_vue_else_directives_have_an_adjacent_condition(self):
        parser = _VueElseAdjacencyParser()
        parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
        self.assertEqual([], parser.errors)

    def test_dashboard_has_one_mounted_hook_that_hides_loader(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        mounted_hooks = re.findall(r"^\s{12}(?:async\s+)?mounted\(\)\s*\{", html, re.MULTILINE)
        created_hooks = re.findall(r"^\s{12}(?:async\s+)?created\(\)\s*\{", html, re.MULTILINE)
        self.assertEqual(1, len(mounted_hooks))
        self.assertEqual(1, len(created_hooks))
        mounted_source = html.split("            async mounted() {", 1)[1].split("            methods: {", 1)[0]
        self.assertIn("loader.style.display = 'none'", mounted_source)
        self.assertIn("window.clearTimeout(window.__codexiaBootTimer)", mounted_source)
        self.assertIn("A inicialização demorou mais do que o esperado.", html)


if __name__ == "__main__":
    unittest.main()
