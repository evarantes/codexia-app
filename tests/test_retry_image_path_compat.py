from __future__ import annotations

import os
import tempfile
import unittest
from typing import List

from scripts import apply_retry_image_path_compat as patch


class RetryImagePathCompatTests(unittest.TestCase):
    def _legacy_source(self) -> str:
        return '''import os\nfrom typing import List\n\ndef _selected_images_ok(urls: List[str], *, min_bytes: int = 1000) -> bool:\n    if not urls:\n        return False\n    try:\n        from app.config import absolute_path_for_static\n    except Exception:\n        absolute_path_for_static = None\n    checked = 0\n    for url in urls:\n        if not url:\n            continue\n        checked += 1\n        if checked > 6:\n            break\n        try:\n            p = absolute_path_for_static(url) if absolute_path_for_static else \"\"\n        except Exception:\n            p = \"\"\n        if not (p and os.path.exists(p) and os.path.getsize(p) >= int(min_bytes or 1)):\n            return False\n    return checked > 0\n\ndef next_function():\n    return True\n'''

    def test_patch_is_idempotent_and_keeps_all_resolvers(self) -> None:
        transformed = patch.patch_youtube(self._legacy_source())
        self.assertIn(patch.MARKER, transformed)
        self.assertIn("absolute_path_for_image, absolute_path_for_static", transformed)
        self.assertIn("if os.path.isfile(raw):", transformed)
        self.assertIn("for resolver in (absolute_path_for_image, absolute_path_for_static):", transformed)
        self.assertEqual(transformed, patch.patch_youtube(transformed))

    def test_absolute_durable_path_is_valid_without_static_remap(self) -> None:
        transformed = patch.patch_youtube(self._legacy_source())
        namespace = {"os": os, "List": List}
        exec(compile(transformed, "<patched-youtube>", "exec"), namespace)
        validator = namespace["_selected_images_ok"]
        with tempfile.TemporaryDirectory() as tmp:
            image_path = os.path.join(tmp, "preserved-scene.jpg")
            with open(image_path, "wb") as handle:
                handle.write(b"x" * 1500)
            self.assertTrue(validator([image_path]))

    def test_missing_absolute_path_remains_blocked(self) -> None:
        transformed = patch.patch_youtube(self._legacy_source())
        namespace = {"os": os, "List": List}
        exec(compile(transformed, "<patched-youtube>", "exec"), namespace)
        validator = namespace["_selected_images_ok"]
        self.assertFalse(validator(["/data/media/images/definitely-not-present.jpg"]))


if __name__ == "__main__":
    unittest.main()
