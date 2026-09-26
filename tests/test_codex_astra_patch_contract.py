"""Native upstream catalog compatibility remains an image gate after the port."""
from pathlib import Path


def test_image_exercises_native_account_catalog_after_runtime_overlay():
    service = Path(__file__).resolve().parents[1] / "services/agent"
    image = (service / "Dockerfile").read_text()
    assertion = "python /tmp/hermes-patches/assert_codex_astra.py /opt/hermes"
    assert image.index("apply /tmp/hermes-patches/hlt_runtime_contract.patch") < image.index(assertion)
    assert image.count(assertion) == 1
    overlay = (service / "hermes_patches/hlt_runtime_contract.patch").read_text()
    # Current upstream owns these equivalent behaviors; do not revive the old backport.
    for native in ("agent/reasoning_effort.py", "agent/model_metadata.py", "hermes_cli/codex_models.py"):
        assert f"+++ b/{native}" not in overlay
