"""Pin the Hermes cache-refresh overlay used by Cleo's hot K2 recovery."""

from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "agent"


def test_image_applies_and_proves_the_prompt_refresh_overlay():
    dockerfile = (SERVICE_DIR / "Dockerfile").read_text(encoding="utf-8")
    check = "apply --check /tmp/hermes-patches/k2_runtime_pack_prompt_refresh.patch"
    apply = "apply /tmp/hermes-patches/k2_runtime_pack_prompt_refresh.patch"
    assertion = (
        "python /tmp/hermes-patches/"
        "assert_k2_runtime_pack_prompt_refresh.py /opt/hermes"
    )

    assert dockerfile.index(check) < dockerfile.index(apply) < dockerfile.index(
        assertion
    )


def test_overlay_invalidates_only_a_new_managed_k2_pack_epoch():
    patch = (
        SERVICE_DIR
        / "hermes_patches"
        / "k2_runtime_pack_prompt_refresh.patch"
    ).read_text(encoding="utf-8")
    added = "\n".join(
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )

    assert patch.count("diff --git") == 2
    assert "active_k2_pack_source" in added
    assert "<!-- source: katailyst2 agents.runtime_pack " in added
    assert "k2_pack_source not in prompt" in added
    assert "return False" in added
    assert "_k2_runtime_pack_read_lock" in added
    assert ".hlt-k2-runtime-pack.lock" in added
    assert "fcntl.LOCK_SH" in added
