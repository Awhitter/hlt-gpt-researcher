#!/usr/bin/env python3
"""Render ``$HERMES_HOME/config.yaml`` for whichever agent this container is.

Hermes reads every setting — model, toolsets, Slack behaviour, MCP mounts —
from a single ``config.yaml`` under ``HERMES_HOME``. Nothing else in this image
writes that file, so without this step the agent boots with no model and no MCP
servers even though every URL and token is present in the environment.

Secrets are written as ``${VAR}`` references rather than literal values —
Hermes expands them at load time, so the persistent Render disk never holds a
token in plaintext.

The security posture here is deliberate and is the reason this file is long.
These agents are reachable by a whole Slack workspace AND read untrusted web pages.
Those two facts together make the upstream defaults unsafe: see SLACK_TOOLSETS.
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

# THE most important setting in this file.
#
# Upstream's default Slack toolset is `hermes-slack`, whose own description is
# "full access for workspace use" and which resolves to _HERMES_CORE_TOOLS —
# terminal, execute_code, write_file, patch, cronjob, computer_use, browser_cdp.
# Left at the default, anyone who can @mention Brian in Slack gets arbitrary
# code execution on this container.
#
# These agents additionally read untrusted third-party web pages, so shell
# access would make one a textbook confused deputy: a hostile page talks the
# model into running a command. Upstream constrains its own webhook toolset for
# exactly this reason.
#
# Excluded on purpose: terminal, execute_code, cronjob, computer_use, browser,
# and `file` (there is no read-only variant — it grants write_file and patch).
SLACK_TOOLSETS: tuple[str, ...] = (
    "web",
    "search",
    "vision",
    "skills",
    "todo",
    "memory",
    "session_search",
    "clarify",
)

# Slash commands any workspace member may run. Everything else — /model, /yolo,
# /rollback, /update — requires being listed in SLACK_ADMIN_USERS. Without an
# admin list configured Hermes disables slash gating entirely and every user can
# run every command, which is why boot warns loudly when it is unset.
#
# Every name here must be a real Hermes command: an entry that does not exist is
# silently inert, so a teammate is refused something you believe you granted.
# `hermes slack manifest` prints the authoritative list (50 commands).
USER_ALLOWED_COMMANDS: tuple[str, ...] = (
    "help",
    "commands",
    "whoami",
    "new",
    "queue",
    "sessions",
    "title",
    "stop",
)

# name -> (url env var, bearer-token env var)
MCP_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("gpt-researcher", "GPTR_MCP_URL", "GPTR_MCP_TOKEN"),
    ("codegraph", "CODEGRAPH_MCP_URL", "CODEGRAPH_MCP_TOKEN"),
    ("katailyst2", "KATAILYST2_MCP_URL", "KATAILYST2_MCP_TOKEN"),
    ("linear", "LINEAR_MCP_URL", "LINEAR_MCP_TOKEN"),
)

# Where the composed briefing lives. MUST be set explicitly: left at ".",
# Hermes' project-context discovery resolves into its own install tree and
# silently loads nothing. grounding.py writes AGENTS.md here at boot.
DEFAULT_GROUNDING_DIR = "/data/hermes/grounding"


def _clean(env: Mapping[str, str], key: str) -> str | None:
    """Env vars set-but-empty are as good as unset for our purposes."""
    return (env.get(key) or "").strip() or None


def _csv(env: Mapping[str, str], key: str) -> list[str]:
    raw = _clean(env, key) or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


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
        server: dict[str, Any] = {
            "url": url,
            "enabled": True,
            # Server-initiated LLM calls are ON by default upstream and billed
            # to us. Only enable per-server for servers we control.
            "sampling": {"enabled": False},
        }
        if _clean(env, token_env):
            server["headers"] = {"Authorization": f"Bearer ${{{token_env}}}"}
        servers[name] = server
    return servers


def build_slack(env: Mapping[str, str]) -> dict[str, Any]:
    """Top-level `slack:` — only the keys Hermes bridges from here.

    Note there are three Slack config namespaces and they are NOT
    interchangeable; keys like rich_blocks belong under platforms.slack.extra
    and are silently ignored if placed here.
    """
    slack: dict[str, Any] = {
        "require_mention": True,
        # Without this, Brian re-engages in old threads he was once mentioned
        # in — surprising, and noisy in a shared workspace.
        "strict_mention": True,
        # Scheduled briefs open their own thread and stay conversational there.
        "cron_continuable_surface": "thread",
    }
    channels = _csv(env, "SLACK_ALLOWED_CHANNELS")
    if channels:
        # Whitelist. Note this covers group DMs but NOT 1:1 DMs.
        slack["allowed_channels"] = ",".join(channels)
    return slack


def build_platforms(env: Mapping[str, str]) -> dict[str, Any]:
    """`platforms.slack` — the namespace that carries the good Slack features."""
    extra: dict[str, Any] = {
        # Real Block Kit: section headers, native tables, nested lists.
        "rich_blocks": True,
        # 👍/👎 on answers, so we learn what lands.
        "feedback_buttons": True,
        "assistant_thread_titles": True,
        "user_allowed_commands": list(USER_ALLOWED_COMMANDS),
    }
    admins = _csv(env, "SLACK_ADMIN_USERS")
    if admins:
        # Distinct list objects: sharing one would make PyYAML emit an
        # anchor/alias pair, which is valid but unreadable in a config a human
        # may need to debug.
        extra["allow_admin_from"] = list(admins)
        extra["group_allow_admin_from"] = list(admins)

    return {
        "slack": {
            # Upstream's default uses Slack's assistant status API, which
            # disables the compose box while Brian thinks.
            "typing_indicator": False,
            # Default posts "♻️ Gateway online" into the workspace on every
            # redeploy. Operator noise for end users.
            "gateway_restart_notification": False,
            "extra": extra,
        }
    }


def build_config(
    env: Mapping[str, str], grounding_dir: str = DEFAULT_GROUNDING_DIR
) -> dict[str, Any]:
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
        "agent": {
            # Fixes the "stops after stating intent" failure on some models.
            "intent_ack_continuation": True,
            # Default 180s posts "still working" into a shared channel every
            # three minutes.
            "gateway_notify_interval": 900,
            # Fast failover to the fallback provider rather than slow retries.
            "api_max_retries": 1,
            "environment_hint": (
                "You run as a hosted Slack bot on Render with no shell, no file "
                "writes and no browser. Reach the estate through your MCP tools "
                "instead. Long work is fine — say what you are doing as you go."
            ),
        },
        # Project-context discovery reads AGENTS.md from here.
        "terminal": {"cwd": grounding_dir},
        # The top-level `toolsets` key is deprecated and ignored upstream; this
        # per-platform map is the one that is actually read.
        "platform_toolsets": {"slack": list(SLACK_TOOLSETS)},
        "slack": build_slack(env),
        "platforms": build_platforms(env),
        "memory": {
            "memory_enabled": True,
            # USER.md is singular — "what the agent knows about the user". With
            # a whole workspace talking to Brian that profile just thrashes.
            "user_profile_enabled": False,
            # Otherwise one person's thread writes durable facts for everyone.
            "write_approval": True,
        },
        "privacy": {"redact_pii": True},
        "security": {"allow_lazy_installs": False},
        # Alec's single-user state.db is already 103 MB; this box is shared.
        "sessions": {"auto_prune": True},
        "session_reset": {"mode": "both", "idle_minutes": 1440},
        # Nothing pinned but the system prompt, rolling summary and recent tail
        # — right for long-lived Slack threads.
        "compression": {"protect_first_n": 0},
        "prompt_caching": {"cache_ttl": "1h"},
        # Scheduled briefs land in their own thread and stay continuable.
        "cron": {"mirror_delivery": True},
        # The curator archives unused skills after 90 days. Ours are shipped in
        # the image and are meant to persist.
        "curator": {"prune_builtins": False},
    }

    fallback = _csv(env, "HERMES_FALLBACK_PROVIDERS")
    if fallback:
        config["fallback_providers"] = fallback

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

    config = build_config(env, grounding_dir=str(home_path / "grounding"))
    servers: dict[str, Any] = config.get("mcp_servers", {})
    summary: dict[str, Any] = {
        "config_path": str(path),
        "model": config["model"]["default"],
        "openrouter_key_present": bool(_clean(env, "OPENROUTER_API_KEY")),
        "slack_toolsets": list(SLACK_TOOLSETS),
        "slack_admins_configured": bool(_csv(env, "SLACK_ADMIN_USERS")),
        "slack_channel_allowlist": bool(_csv(env, "SLACK_ALLOWED_CHANNELS")),
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
        print(f"[agent] config {key}: {value}")
