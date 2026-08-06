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
#
# `cronjob` stays out even though it is tempting for the weekly brief: a
# scheduled job runs UNATTENDED with the agent's whole toolset, so anyone who
# can @mention her could leave something running that files Linear issues with
# nobody watching. Schedule briefs from the operator side instead.
#
# `delegation` is in, and it is safe here for a specific reason —
# `tools/delegate_tool.py`: "Subagents inherit the parent's toolsets", with
# child-blocked tools and `delegation` itself stripped. A child therefore gets
# THIS narrow list, not the full CLI toolset, so it is not an escape hatch.
# Verify that still holds before trusting it after an upstream bump.
#
# `search` was dropped as redundant: it is web_search alone, and `web` already
# bundles web_search + web_extract.
SLACK_TOOLSETS: tuple[str, ...] = (
    "web",
    "vision",
    "skills",
    "todo",
    "memory",
    "session_search",
    "clarify",
    # Break a big "explain the whole architecture" ask into parallel readers.
    "delegation",
    # Diagrams and a listen-later summary — image_generate needs FAL_KEY,
    # text_to_speech uses ElevenLabs when ELEVENLABS_API_KEY is set.
    "image_gen",
    "tts",
)

# One-tap starters at the Agent/Assistant entry point (Slack caps this at 4).
# Written as a new hire would ask them, not as an insider would.
SUGGESTED_PROMPTS: tuple[dict[str, str], ...] = (
    {
        "title": "What shipped this week?",
        "message": "What shipped in Nursing Mastery this week, and what's still in flight?",
    },
    {
        "title": "Explain a subsystem",
        "message": "I'm new here. Explain how the job board gets its jobs, and point me at the files.",
    },
    {
        "title": "Where do I start?",
        "message": "I just joined. What are the three things I should understand first about this codebase, and what will confuse me?",
    },
    {
        "title": "What's on the board?",
        "message": "What's in progress on the NUR team right now, and is anything stuck?",
    },
)

# The per-surface guidance Hermes injects into the system prompt's STABLE tier
# (see upstream `developer-guide/prompt-assembly.md`). `append` keeps the
# built-in Slack hint and adds ours after it; a byte-stable string here does not
# break prompt caching. This is the supported way to shape behaviour per
# surface — the alternative is editing her SOUL, which applies everywhere.
SLACK_PLATFORM_HINT = (
    "You are answering in Slack, for a team that did NOT build this system, "
    "and most of them are not engineers.\n"
    "Answer in the register of the person asking. Their job decides what an "
    "answer is: a marketing question gets audience, voice and funnel; a "
    "delivery question gets projects, dates and owners. NEVER open a reply to "
    "a non-engineer with an internal name — a file path, a repo label, a "
    "D-number, a system codename. Those are citations you add at the end.\n"
    "Asked what you can do for someone, answer in THEIR work, not as an "
    "inventory of your tools.\n"
    "Query the source before you describe it. Saying a system 'holds our voice "
    "and personas' without opening it looks like an answer and contains none.\n"
    "Ground every answer, and put the evidence at the END as a short Sources "
    "list — the identifier, file or registry ref you actually opened — instead "
    "of scattering them mid-sentence where they break the reading.\n"
    "When you do not know, say so and say how you would find out. That beats a "
    "confident guess.\n"
    "Keep it to a few short paragraphs; this is a chat message, not a "
    "document. Lead with the answer, then the evidence.\n"
    "When a picture would explain a flow faster than prose, offer to generate "
    "one. When someone asks for a summary they can take away, offer audio."
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
    # How the funnel is actually performing. Read-only; the grounding
    # carries the caveat that ~64% of events are ScraperVault machine
    # traffic, so an unfiltered number is worse than no number.
    ("posthog", "POSTHOG_MCP_URL", "POSTHOG_MCP_TOKEN"),
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


def build_home_channel(env: Mapping[str, str]) -> dict[str, Any] | None:
    """`platforms.slack.home_channel` — where cron output lands.

    Unset, Hermes posts a "No home channel is set" notice at the start of every
    new session and tells the user to type `/hermes sethome`. That was the FIRST
    thing a new teammate ever saw from this agent, and the command it names does
    not work unless the Slack manifest declares `/hermes` — ours did not.

    Setting it here removes the notice for everyone, with nobody having to run
    anything. Shape matches upstream `HomeChannel.to_dict()`.

    Format: `SLACK_HOME_CHANNEL="C0BN349TRU7|#cleo"` (id, optional label).
    A value with no channel id is ignored rather than written half-formed — a
    malformed home channel silently breaks cron delivery.
    """
    raw = _clean(env, "SLACK_HOME_CHANNEL")
    if not raw:
        return None
    chat_id, _, name = raw.partition("|")
    chat_id = chat_id.strip()
    if not chat_id:
        return None
    return {
        "platform": "slack",
        "chat_id": chat_id,
        "name": name.strip() or chat_id,
    }


def build_platforms(env: Mapping[str, str]) -> dict[str, Any]:
    """`platforms.slack` — the namespace that carries the good Slack features."""
    extra: dict[str, Any] = {
        # Hermes defaults every messaging platform to a pairing/allowlist policy
        # and DENIES unknown senders. For a workspace bot that means silence for
        # everyone but a named list — so DMs and channels are opened explicitly
        # here, and GATEWAY_ALLOW_ALL_USERS carries the matching env opt-in.
        #
        # This is only safe because the toolset above is locked to read-only
        # research tools and privileged slash commands are admin-gated. Do not
        # open these without both.
        "dm_policy": "open",
        "group_policy": "open",
        # Real Block Kit: section headers, native tables, nested lists.
        "rich_blocks": True,
        # 👍/👎 on answers, so we learn what lands.
        "feedback_buttons": True,
        "assistant_thread_titles": True,
        "user_allowed_commands": list(USER_ALLOWED_COMMANDS),
        # Slack renders these as one-tap starters at the Agent/Assistant entry
        # point. A new engineer opening her for the first time should not have
        # to guess what she is for — max 4, and they are the four questions
        # this whole agent exists to answer.
        "suggested_prompts": list(SUGGESTED_PROMPTS),
        # Shown in the composer footer while she works. The default is
        # "is thinking...", which says nothing about a bot that may be doing a
        # multi-minute read across five repos.
        "typing_status_text": "is digging through the estate…",
        # "none" ignores every bot; "mentions" accepts a bot message only when
        # that message itself @mentions her. Agents posting on a human's behalf
        # (the Claude Slack app, other HLT bots) carry an app id and are
        # otherwise dropped in silence — which is exactly what made her look
        # dead during setup while she was working fine. "mentions" is upstream's
        # documented safest bot-to-bot mode: it cannot loop, because a reply
        # without an explicit mention is still ignored.
        "allow_bots": "mentions",
    }
    admins = _csv(env, "SLACK_ADMIN_USERS")
    if admins:
        # Distinct list objects: sharing one would make PyYAML emit an
        # anchor/alias pair, which is valid but unreadable in a config a human
        # may need to debug.
        extra["allow_admin_from"] = list(admins)
        extra["group_allow_admin_from"] = list(admins)

    slack: dict[str, Any] = {
        # Upstream's default uses Slack's assistant status API, which
        # disables the compose box while Brian thinks.
        "typing_indicator": False,
        # Default posts "♻️ Gateway online" into the workspace on every
        # redeploy. Operator noise for end users.
        "gateway_restart_notification": False,
        "extra": extra,
    }
    home = build_home_channel(env)
    if home:
        slack["home_channel"] = home
    return {"slack": slack}


def slack_toolsets(servers: Mapping[str, Any]) -> list[str]:
    """The Slack allowlist, including one entry per configured MCP server.

    `platform_toolsets` is an ALLOWLIST, and every MCP server registers its
    tools under a dynamic toolset named `mcp-<server>` (`tools/mcp_tool.py`:
    `toolset_name = f"mcp-{self.name}"`). Listing the servers under
    `mcp_servers` connects them; it does NOT grant their tools to a platform
    whose toolset list omits them.

    Cleo shipped that way. `/health` reported
    `mcp_mounted: [codegraph, gpt-researcher, katailyst2, linear]` and
    `mcp_without_token: []` — both true — while she had not one of their tools
    and told a teammate: "they're not actually available to me in this
    session." Connected is not granted.

    Derived from the servers actually configured, so adding an MCP server
    grants it automatically and an unconfigured one never appears.
    """
    return list(SLACK_TOOLSETS) + sorted(f"mcp-{name}" for name in servers)


def build_config(
    env: Mapping[str, str], grounding_dir: str = DEFAULT_GROUNDING_DIR
) -> dict[str, Any]:
    servers = build_mcp_servers(env)
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
        # Upstream default is `edge`, and having ELEVENLABS_API_KEY set is
        # deliberately NOT enough — "Inference credentials do not imply consent
        # to paid speech generation" (tools/tts_tool.py). So the tool gate
        # `check_tts_requirements` returned False and text_to_speech was
        # unavailable every turn while /health cheerfully reported the key
        # present. Naming the provider is the opt-in.
        "tts": {"provider": "elevenlabs"},
        # web_search/web_extract are gated by `check_web_api_key`, which needs a
        # backend that actually resolves. With none configured the whole `web`
        # toolset was dead — she could not search at all. `ddgs` is in upstream's
        # backend list and needs no API key, so it is the honest default; set
        # WEB_SEARCH_BACKEND to move to a paid one (tavily, exa, firecrawl…).
        "web": {"backend": _clean(env, "WEB_SEARCH_BACKEND") or "ddgs"},
        # The top-level `toolsets` key is deprecated and ignored upstream; this
        # per-platform map is the one that is actually read.
        "platform_toolsets": {"slack": slack_toolsets(servers)},
        # Per-surface prompt guidance. Top-level key, NOT under `platforms` —
        # a third Slack namespace, and putting it in the wrong one is silently
        # ignored like every other misplaced Slack key in this file.
        "platform_hints": {"slack": {"append": SLACK_PLATFORM_HINT}},
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
        # What Hermes was ACTUALLY handed, mcp-* grants included — not the
        # static tuple. Reporting the tuple hid the fact that four mounted MCP
        # servers had none of their tools granted.
        "slack_toolsets": config.get("platform_toolsets", {}).get(
            "slack", list(SLACK_TOOLSETS)
        ),
        "slack_admins_configured": bool(_csv(env, "SLACK_ADMIN_USERS")),
        "slack_channel_allowlist": bool(_csv(env, "SLACK_ALLOWED_CHANNELS")),
        "slack_senders_allowed": (
            "all"
            if (_clean(env, "GATEWAY_ALLOW_ALL_USERS") or "").lower() == "true"
            else ("allowlist" if _csv(env, "SLACK_ALLOWED_USERS") else "none")
        ),
        # The image and audio toolsets are loaded either way; without their key
        # the tool exists and fails at call time, so the agent offers a diagram
        # and then cannot produce one. Report the credential, not the toolset.
        "media_backends": {
            "image_generate": bool(_clean(env, "FAL_KEY")),
            "text_to_speech": bool(_clean(env, "ELEVENLABS_API_KEY")),
        },
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
