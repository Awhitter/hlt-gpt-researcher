"""Contract tests for the hlt-hermes boot config renderer.

The service this guards spent a week deployed and green while doing nothing,
because the config Hermes reads was documented but never written. These tests
pin the parts that made it silent: that a mount only appears when it is really
configured, that tokens stay out of the persistent disk, and that the summary
`/health` reports is derived from what was written rather than from which env
vars happen to be set.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "hermes"


def _load(module_name: str, path: Path):
    """Load a service module by path, WITHOUT touching sys.path.

    Prepending a service directory to sys.path leaks into every test module
    collected after this one — `services/codegraph/server.py` shadowing the
    `server` package is exactly how that bites.
    """
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render_config = _load("hlt_hermes_render_config", SERVICE_DIR / "render_config.py")

# The interpolation pattern Hermes itself uses (hermes_cli/config.py).
HERMES_ENV_REF = re.compile(r"\$\{([^}]+)\}")

FULL_ENV = {
    "OPENROUTER_API_KEY": "sk-or-secret",
    "GPTR_MCP_URL": "https://gptr.example/mcp",
    "GPTR_MCP_TOKEN": "gptr-secret",
    "CODEGRAPH_MCP_URL": "https://codegraph.example/mcp",
    "CODEGRAPH_MCP_TOKEN": "codegraph-secret",
    "KATAILYST2_MCP_URL": "https://k2.example/api/mcp",
    "KATAILYST2_MCP_TOKEN": "k2-secret",
    "LINEAR_MCP_URL": "https://linear.example/mcp",
    "LINEAR_MCP_TOKEN": "linear-secret",
}


def test_mounts_only_configured_servers():
    env = {"GPTR_MCP_URL": "https://gptr.example/mcp", "GPTR_MCP_TOKEN": "t"}
    servers = render_config.build_mcp_servers(env)
    assert set(servers) == {"gpt-researcher"}


def test_url_without_token_mounts_without_auth_header():
    """A public/oauth server should still mount, just without a bearer header."""
    servers = render_config.build_mcp_servers({"LINEAR_MCP_URL": "https://linear.example/mcp"})
    assert servers["linear"]["url"] == "https://linear.example/mcp"
    assert "headers" not in servers["linear"]


def test_empty_string_env_is_treated_as_unset():
    servers = render_config.build_mcp_servers({"GPTR_MCP_URL": "   ", "GPTR_MCP_TOKEN": "t"})
    assert servers == {}


def test_tokens_are_env_references_not_literals(tmp_path):
    render_config.render(env=FULL_ENV, home=tmp_path)
    written = (tmp_path / "config.yaml").read_text(encoding="utf-8")

    for secret in ("gptr-secret", "codegraph-secret", "k2-secret", "linear-secret", "sk-or-secret"):
        assert secret not in written, f"{secret} was written to the persistent disk"

    header = yaml.safe_load(written)["mcp_servers"]["gpt-researcher"]["headers"]["Authorization"]
    assert HERMES_ENV_REF.search(header).group(1) == "GPTR_MCP_TOKEN"


def test_generated_config_matches_hermes_schema(tmp_path):
    render_config.render(env=FULL_ENV, home=tmp_path)
    config = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))

    # Top-level keys Hermes actually reads. `gateway:` and `memory.seed_paths`
    # were in the old example file and are not real config keys.
    assert config["model"]["provider"] == "openrouter"
    assert config["memory"]["memory_enabled"] is True
    assert config["slack"]["require_mention"] is True
    assert set(config["mcp_servers"]) == {"gpt-researcher", "codegraph", "katailyst2", "linear"}
    assert "gateway" not in config


def test_model_override_precedence(tmp_path):
    assert render_config.render(env={}, home=tmp_path)["model"] == render_config.DEFAULT_MODEL

    env = {"HERMES_MODEL": "anthropic/claude-opus-5", "OPENROUTER_MODEL": "ignored/model"}
    assert render_config.render(env=env, home=tmp_path)["model"] == "anthropic/claude-opus-5"


def test_summary_reports_what_was_actually_mounted(tmp_path):
    summary = render_config.render(
        env={"GPTR_MCP_URL": "https://gptr.example/mcp", "LINEAR_MCP_URL": "https://l.example/mcp"},
        home=tmp_path,
    )
    assert summary["written"] is True
    assert summary["mcp_mounted"] == ["gpt-researcher", "linear"]
    assert summary["mcp_without_token"] == ["gpt-researcher", "linear"]
    assert sorted(summary["mcp_unconfigured"]) == ["codegraph", "katailyst2"]
    assert summary["openrouter_key_present"] is False


def test_hand_edited_config_is_preserved(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("model:\n  default: hand-tuned\n", encoding="utf-8")

    summary = render_config.render(env=FULL_ENV, home=tmp_path)

    assert summary["preserved_operator_config"] is True
    assert summary["written"] is False
    assert "hand-tuned" in path.read_text(encoding="utf-8")


def test_own_config_is_refreshed_on_reboot(tmp_path):
    render_config.render(env={"GPTR_MCP_URL": "https://gptr.example/mcp"}, home=tmp_path)
    summary = render_config.render(env=FULL_ENV, home=tmp_path)

    assert summary["preserved_operator_config"] is False
    assert summary["written"] is True
    config = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert "codegraph" in config["mcp_servers"]


def test_corrupt_config_is_replaced_not_inherited(tmp_path):
    (tmp_path / "config.yaml").write_text("{{ not: valid: yaml", encoding="utf-8")

    summary = render_config.render(env=FULL_ENV, home=tmp_path)

    assert summary["written"] is True
    assert yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))["model"]


def test_render_creates_missing_home(tmp_path):
    home = tmp_path / "nested" / "hermes"
    render_config.render(env=FULL_ENV, home=home)
    assert (home / "config.yaml").exists()
