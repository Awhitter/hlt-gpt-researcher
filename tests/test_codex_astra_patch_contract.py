"""Keep the stable Astra backport attached to the real Linux image gates."""

import unittest
from pathlib import Path


class CodexAstraImageContract(unittest.TestCase):
    def test_image_applies_and_exercises_astra_compatibility(self):
        service = Path(__file__).resolve().parents[1] / "services" / "agent"
        dockerfile = (service / "Dockerfile").read_text(encoding="utf-8")
        overlay = "codex_astra_account_catalog.patch"
        check = f"apply --check /tmp/hermes-patches/{overlay}"
        apply = f"apply /tmp/hermes-patches/{overlay}"
        assertion = "python /tmp/hermes-patches/assert_codex_astra.py /opt/hermes"

        self.assertLess(dockerfile.index(check), dockerfile.index(apply))
        self.assertLess(dockerfile.index(apply), dockerfile.index(assertion))
        self.assertEqual(dockerfile.count(assertion), 1)
        self.assertTrue((service / "hermes_patches" / overlay).is_file())
        self.assertTrue((service / "hermes_patches" / "assert_codex_astra.py").is_file())

    def test_backport_leaves_provider_refresh_and_transport_overlays_separate(self):
        service = Path(__file__).resolve().parents[1] / "services" / "agent"
        overlay = (service / "hermes_patches" / "codex_astra_account_catalog.patch").read_text()
        changed_files = [line.removeprefix("+++ b/") for line in overlay.splitlines()
                         if line.startswith("+++ b/")]
        self.assertEqual(set(changed_files), {
            "agent/reasoning_effort.py",
            "agent/model_metadata.py",
            "hermes_cli/codex_models.py",
        })


if __name__ == "__main__":
    unittest.main()

