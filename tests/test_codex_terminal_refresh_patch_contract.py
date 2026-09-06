"""Pin the exact Codex recovery overlay and its dependency-free build proof."""

import unittest
from pathlib import Path


class CodexRefreshImageContract(unittest.TestCase):
    def test_image_applies_and_exercises_the_refresh_overlay(self):
        service = Path(__file__).resolve().parents[1] / "services" / "agent"
        dockerfile = (service / "Dockerfile").read_text(encoding="utf-8")
        patch = "codex_terminal_refresh.patch"
        check = f"apply --check /tmp/hermes-patches/{patch}"
        apply = f"apply /tmp/hermes-patches/{patch}"
        assertion = "python /tmp/hermes-patches/assert_codex_terminal_refresh.py /opt/hermes"

        self.assertLess(dockerfile.index(check), dockerfile.index(apply))
        self.assertLess(dockerfile.index(apply), dockerfile.index(assertion))
        self.assertEqual(dockerfile.count(assertion), 1)
        self.assertTrue((service / "hermes_patches" / patch).is_file())
        self.assertTrue((service / "hermes_patches" / "assert_codex_terminal_refresh.py").is_file())


if __name__ == "__main__":
    unittest.main()
