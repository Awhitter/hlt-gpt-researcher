#!/usr/bin/env python3
"""Render ``$HERMES_HOME/config.yaml`` from the Render environment.

Hermes reads its model, Slack behaviour and MCP mounts from a single
``config.yaml`` under ``HERMES_HOME``. Nothing else in this image writes that
file, so without this step the agent boots with no model and no MCP servers
even though every URL and token is already present in the environment.

Secrets are written as ``${VAR}`` references rather than literal values —
Hermes expands them at load time, so the persistent Render disk never holds a
token in plaintext.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import yaml

# Marks a config this script owns. A file without the marker was written by a
# human, so we leave it alone rather than overwriting their work on next boot.
GENERATED_BY = "hlt-render-boot"

# Verified present on OpenRouter; override per-service with HERMES_MODEL.
DEFAULT_MODEL = "anthropic/claude-sonnet-5"

# name -> (url env var, bearer-token env var)
MCP_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("gpt-researcher", "GPTR_MCP_URL", "GPTR_MCP_TOKEN"),
    ("codegraph", "CODEGRAPH_MCP_URL", "CODEGRAPH_MCP_TOKEN"),
    ("katailyst2", "KATAILYST2_MCP_URL", "KATAILYST2_MCP_TOKEN"),
    ("linear", "LINEAR_MCP_URL", "LINEAR_MCP_TOKEN"),
)


def _clean(env: Mapping[str, str], key: str) -> str | None:
    """Env vars set-but-empty are as good as unset for our purposes."""
    return (env.get(key) or "").strip() or None


def build_mcp_servers(env: Mapping[str, str]) -> dict[str, Any]:
    """Mount only the servers whose URL is actually configured.

    A half-configured environment should yield a smaller working config, not a
    config with dangling entries that fail every tool call at runtime.
    """
    servers: dict[str, Any] = {}
    for name, url_env, token_env in MCP_TARGETS:
        url = _clean(env, url_env)
        if not url:
            continue
        server: dict[str, Any] = {"url": url, "enabled": True}
        if _clean(env, token_env):
            server["headers"] = {"Authorization": f"Bearer ${{{token_env}}}"}
        servers[name] = server
    return servers


def build_config(env: Mapping[str, str]) -> dict[str, Any]:
    config: dict[str, Any] = {
        "_generated_by": GENERATED_BY,
        "model": {
            "provider": "openrouter",
            "default": (
                _clean(env, "HERMES_MODEL")
                or _clean(env, "OPENROUTER_MODEL")
                or DEFAULT_MODEL
            ),
        },
        "memory": {"memory_enabled": True},
        # Shared workspace: answer DMs freely, but only speak in a channel when
        # someone actually asks for it.
        "slack": {"require_mention": True},
    }
    servers = build_mcp_servers(env)
    if servers:
        config["mcp_servers"] = servers
    return config


def _existing_is_operator_owned(path: Path) -> bool:
    try:
        existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        # Unreadable or corrupt: treat as ours and rewrite a known-good file.
        return False
    return isinstance(existing, dict) and existing.get("_generated_by") != GENERATED_BY


def render(
    env: Mapping[str, str] | None = None, home: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    """Write config.yaml and return a secret-free summary for ``/health``."""
    env = os.environ if env is None else env
    home_path = Path(home or _clean(env, "HERMES_HOME") or "/data/hermes")
    home_path.mkdir(parents=True, exist_ok=True)
    path = home_path / "config.yaml"

    config = build_config(env)
    servers: dict[str, Any] = config.get("mcp_servers", {})
    summary: dict[str, Any] = {
        "config_path": str(path),
        "model": config["model"]["default"],
        "openrouter_key_present": bool(_clean(env, "OPENROUTER_API_KEY")),
        "mcp_mounted": sorted(servers),
        "mcp_unconfigured": [n for n, _, _ in MCP_TARGETS if n not in servers],
        "mcp_without_token": sorted(
            n for n, s in servers.items() if "headers" not in s
        ),
        "written": False,
        "preserved_operator_config": False,
    }

    if path.exists() and _existing_is_operator_owned(path):
        summary["preserved_operator_config"] = True
        return summary

    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    summary["written"] = True
    return summary


if __name__ == "__main__":
    for key, value in render().items():
        print(f"[hermes] config {key}: {value}")
