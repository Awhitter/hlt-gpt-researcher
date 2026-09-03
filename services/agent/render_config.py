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

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

# Marks a config this script owns. A file without the marker was written by a
# human, so we leave it alone rather than overwriting their work on next boot.
GENERATED_BY = "hlt-render-boot"

# Cleo runs primarily on the owner's existing SuperGrok subscription through
# Hermes' xAI device-code OAuth provider. OpenRouter remains a recoverable
# fallback, not the billable default. ``HERMES_INFERENCE_PROVIDER`` is the
# upstream Hermes provider override, so operators can still switch without a
# code change.
DEFAULT_PROVIDER = "xai-oauth"
# grok-4.6 (released 2026-08-12) supersedes 4.5 on the same subscription at the
# same list price, and scores 60.9 on Artificial Analysis' intelligence index
# against 4.5's 55.8 — a free +5.1. Nothing here was watching for it, which is
# the whole reason katailyst2 now runs a release rail; Cleo is outside that
# loop's reach, so her pin is checked by hand at the same cadence.
DEFAULT_MODEL = "grok-4.6"
DEFAULT_MAX_TOKENS = 32_768
# A live Nursing Mastery funnel brief completed in 20 model iterations. Leaving
# Hermes at its upstream 500-turn default gives one externally-triggered run
# enough room to consume a subscription rate window long after the useful work
# should have converged. Twenty-four keeps measured headroom for API/K2 work;
# interactive Slack gets the stricter surface cap below.
DEFAULT_MAX_TURNS = 24
DEFAULT_SLACK_MAX_TURNS = 7
DEFAULT_SLACK_TOOL_ROUNDS = 5
# Grok's large context window otherwise delayed automatic compression until the
# prompt had grown past 180k tokens. An absolute trigger bounds repeated input
# independently of whichever model route is active; Hermes still preserves the
# initial request, rolling summary, and recent tail.
DEFAULT_COMPRESSION_THRESHOLD_TOKENS = 80_000
# Hermes keeps the full payload on the durable agent disk and replaces the
# model-visible result with a 1,500-character preview once this threshold is
# crossed. K2's nested discovery bridge returned several 19K-53K responses in
# one ordinary funnel read; upstream's 50K MCP default let most of them stack
# inside the prompt before compression could help.
DEFAULT_MCP_RESULT_SIZE_CHARS = 16_000
DEFAULT_TOOL_SEARCH_LIMIT = 3
MAX_TOOL_SEARCH_LIMIT = 8
TOOL_LISTING_MAX_TOKENS = 2_000
PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "xai-oauth": DEFAULT_MODEL,
    "openai-codex": "gpt-5.6-sol",
    "openrouter": "moonshotai/kimi-k3",
}

# Recovery order, not a second policy engine. Hermes walks this list only when
# the active route fails after its bounded retry. Every entry is a real model
# id from the provider's current catalog; provider-only strings are not a valid
# Hermes fallback contract and are intentionally never emitted.
#
# xAI stays primary because the owner already pays for SuperGrok. When an
# XAI_API_KEY is configured, the SAME model over the plain api-key provider is
# the first recovery route — the OAuth token is a six-hour credential that has
# already expired unnoticed once (2026-08-18, 68 hours dark), and a key rung
# means an auth failure degrades billing, never the model. OpenRouter is the
# independent safety net. ChatGPT subscription OAuth is intentionally not a
# default rung: its refresh token is single-use and this long-lived service
# cannot renew it without an operator device-code login. A stale Codex profile
# previously emitted refresh 401s at every boot while health still called the
# route available. Operators can still add ``openai-codex`` explicitly through
# ``HERMES_FALLBACK_PROVIDERS`` after a fresh login and bounded private canary.
DEFAULT_FALLBACK_PROVIDERS: tuple[dict[str, str], ...] = (
    {"provider": "openrouter", "model": "moonshotai/kimi-k3"},
    {"provider": "openrouter", "model": "qwen/qwen3.8-max"},
    {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro-0813"},
)
# Hermes' plain `xai` provider reads XAI_API_KEY from env (auth.py
# ProviderConfig). Emitting the route without the key would burn a failover
# hop on a rung that cannot authenticate, so it joins only when the key is
# actually present on the service.
XAI_API_KEY_FALLBACK: dict[str, str] = {"provider": "xai", "model": DEFAULT_MODEL}

# Registry identity is deliberately separate from the runtime name. Cleo's
# durable capabilities and graph links live in K2; this compact pointer lets the
# hosted persona load them at task time instead of copying them into prompts.
AGENT_REFS: dict[str, str] = {"cleo": "agent:cleo"}

# Runtime identity is host metadata, not an argument to K2 discovery tools.
# Keeping it explicit in health output lets an operator prove that the canonical
# Cleo entity is running in the intended Hermes body without teaching the model
# a made-up ``runtimeLane`` parameter.
AGENT_RUNTIME_LANES: dict[str, str] = {"cleo": "hermes", "brian": "hermes"}

# THE most important setting in this file.
#
# Upstream's default Slack toolset is `hermes-slack`, whose own description is
# "full access for workspace use" and which resolves to _HERMES_CORE_TOOLS —
# terminal, execute_code, write_file, patch, cronjob, computer_use, browser_cdp.
# Left at the default, anyone who can @mention the agent in Slack gets arbitrary
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
    # Read only one bounded page from a result Hermes already persisted under
    # its own spillover directory. This preserves on-demand depth without
    # granting Slack the general file/write toolset.
    "hlt-context",
)

# One-tap Nursing Mastery product starters at the Agent/Assistant entry point.
SUGGESTED_PROMPTS: tuple[dict[str, str], ...] = (
    {
        "title": "Choose the next product bet",
        "message": "What should Nursing Mastery build or change next? Use current evidence and make the call.",
    },
    {
        "title": "Read the funnel",
        "message": "Read the Nursing Mastery funnel. Name the bottleneck and the one decision I should make.",
    },
    {
        "title": "Take a product mission",
        "message": "Take one high-leverage Nursing Mastery mission through research, decision, and a finished artifact.",
    },
)

# The three visible Agent View actions belong to the same cross-file contract
# as the starters above. Keeping them here gives the manifest validator one
# runtime-owned source instead of accepting arbitrary copy with the right shape.
AGENT_VIEW_ACTIONS: tuple[dict[str, str], ...] = (
    {
        "name": "Build the product plan",
        "description": "Turn current evidence into one decision-ready plan with owners and proof.",
    },
    {
        "name": "Read the funnel",
        "description": "Find the Nursing Mastery bottleneck and recommend the next move.",
    },
    {
        "name": "Finish the useful artifact",
        "description": "Research, create, and verify the brief, prototype, or decision.",
    },
)

# The per-surface guidance Hermes injects into the system prompt's STABLE tier
# (see upstream `developer-guide/prompt-assembly.md`). `append` keeps the
# built-in Slack hint and adds ours after it; a byte-stable string here does not
# break prompt caching. This is the supported way to shape behaviour per
# surface — the alternative is editing her SOUL, which applies everywhere.
SLACK_PLATFORM_HINT = (
    "You are answering a team in Slack; most people did not build the system. "
    "Lead with the useful result in their register. No 'Sources:' footer: cite "
    "at most one short parenthetical for a claim a reader would doubt, and let "
    "ordinary answers stand on their own.\n"
    "Take ownership of a broad request: define a practical done condition, do "
    "the safe work available now, and return the answer or artifact rather than "
    "a tool inventory or a menu of questions.\n"
    "Use current source authority. If a capability is not visible, search K2's "
    "progressive catalog and try one credible alternate before reporting the "
    "exact access gap. Do not claim a handoff or delivery without readback.\n"
    "Hermes already exposes K2 progressively: use host tool_search, describe one "
    "direct mcp__katailyst2__<verb>, then call it. Prefer that direct verb over "
    "K2's nested tool_search/tool_describe/tool_execute compatibility bridge. "
    "If the bridge is genuinely needed, request tool_describe detailLevel "
    "'summary' first and ask for action plus schema only for the exact action "
    "you are ready to invoke.\n"
    "PostHog is exposed through mcp__posthog__exec as a CLI bridge. Use search "
    "<regex> (or tools), info <tool_name> once, schema <tool_name> <field_path> "
    "only for hinted complex fields, then call --json <tool_name> <json_input>. "
    "Reuse the discovered contract; do not guess action names or wrapper shapes.\n"
    "When a tool result is marked persisted-output, use read_spillover with the "
    "saved path plus an offset and limit to retrieve only the needed page; do not "
    "repeat the remote request just to recover omitted output.\n"
    "For a specialist handoff, mention the named agent with a bounded output "
    "and keep working on your part; reconcile their reply instead of waiting.\n"
    "For exact interface text or labeled structure, prefer a deterministic "
    "prototype or diagram tool; use image generation for imagery. A text diagram "
    "is a fallback, not the requested high-fidelity mockup.\n"
    "Slack already shows your live work in its ephemeral status. "
    "Do not narrate each tool call as a separate message, do not use a send-message "
    "tool to deliver the answer you are currently composing, and return exactly one "
    "final answer. Keep ordinary replies concise. Use at most five tool-calling rounds "
    "for a Slack turn: take the highest-signal reads first, never repeat a discovery "
    "query, then synthesize from evidence already collected and call missing values "
    "unknown. Reserve the final model response for synthesis.\n"
    "When the user requests a Slack table, emit a plain Markdown pipe table with a "
    "header and separator row, never a fenced code block. If the table would not stay "
    "compact, use short bullets instead."
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


def _fallback_entry(value: object) -> dict[str, str] | None:
    """Normalize one operator-supplied fallback without guessing a model.

    Hermes' pinned contract is a list of objects with BOTH ``provider`` and
    ``model``. The old renderer emitted provider-name strings, which Hermes
    quietly filtered out. We accept a JSON object or the concise
    ``provider:model`` form, and retain provider-only input only for providers
    whose default model is explicitly pinned above.
    """
    if isinstance(value, dict):
        provider = str(value.get("provider") or "").strip().lower()
        model = str(value.get("model") or "").strip()
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if ":" in raw:
            provider, model = raw.split(":", 1)
            provider, model = provider.strip().lower(), model.strip()
        else:
            provider = raw.lower()
            model = PROVIDER_DEFAULT_MODELS.get(provider, "")
    else:
        return None
    if not provider or not model:
        return None
    return {"provider": provider, "model": model}


def fallback_providers(
    env: Mapping[str, str], *, primary_provider: str, primary_model: str
) -> list[dict[str, str]]:
    """Return the exact ordered route objects the pinned Hermes runtime reads.

    ``HERMES_FALLBACK_PROVIDERS`` may be either JSON or a comma-separated list
    of ``provider:model`` entries. Unset means the HLT recovery chain above.
    Invalid entries are skipped, duplicates collapse, and the primary route is
    removed so recovery never loops back to the backend that just failed.
    """
    raw = _clean(env, "HERMES_FALLBACK_PROVIDERS")
    default_values = [dict(route) for route in DEFAULT_FALLBACK_PROVIDERS]
    if _clean(env, "XAI_API_KEY"):
        default_values.insert(0, dict(XAI_API_KEY_FALLBACK))
    values: list[object]
    explicitly_disabled = False
    if raw is None:
        values = default_values
    elif raw.startswith(("[", "{")):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            values = default_values
        else:
            explicitly_disabled = isinstance(decoded, list) and not decoded
            values = decoded if isinstance(decoded, list) else [decoded]
    else:
        values = [part.strip() for part in raw.split(",") if part.strip()]

    primary = (primary_provider.strip().lower(), primary_model.strip().lower())
    seen: set[tuple[str, str]] = set()
    routes: list[dict[str, str]] = []
    for value in values:
        route = _fallback_entry(value)
        if route is None:
            continue
        identity = (route["provider"].lower(), route["model"].lower())
        if identity == primary or identity in seen:
            continue
        seen.add(identity)
        routes.append(route)
    if raw is not None and not routes and not explicitly_disabled:
        return fallback_providers(
            {}, primary_provider=primary_provider, primary_model=primary_model
        )
    return routes


def agent_ref(env: Mapping[str, str]) -> str | None:
    configured = _clean(env, "HLT_AGENT_REF")
    if configured:
        return configured
    agent_id = (_clean(env, "AGENT_ID") or "cleo").lower()
    return AGENT_REFS.get(agent_id)


def runtime_lane(env: Mapping[str, str]) -> str:
    agent_id = (_clean(env, "AGENT_ID") or "cleo").lower()
    return AGENT_RUNTIME_LANES.get(agent_id, "hermes")


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
        # Without this, the agent re-engages in old threads it was once mentioned
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
        # Slack's Assistant status line is ephemeral. Keep it on so a long
        # research turn feels alive without adding one progress post per tool.
        "typing_indicator": True,
        # Default posts "♻️ Gateway online" into the workspace on every
        # redeploy. Operator noise for end users.
        "gateway_restart_notification": False,
        "extra": extra,
    }
    home = build_home_channel(env)
    if home:
        slack["home_channel"] = home
    platforms: dict[str, Any] = {"slack": slack}
    if _clean(env, "OPENCLAW_HQ_HOOK_TOKEN"):
        # K2's existing external-run adapter speaks the OpenClaw hook envelope.
        # Keep the public compatibility route on this wrapper and dispatch it
        # over loopback into Hermes' native run lifecycle. The API server is
        # never exposed on Render's public interface.
        platforms["api_server"] = {
            "enabled": True,
            "extra": {
                "host": "127.0.0.1",
                "port": 8642,
            },
        }
    return platforms


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


def api_server_toolsets(servers: Mapping[str, Any]) -> list[str]:
    """External-run allowlist without Slack-session-local spillover access."""
    return [
        toolset for toolset in slack_toolsets(servers) if toolset != "hlt-context"
    ]


def build_config(
    env: Mapping[str, str], grounding_dir: str = DEFAULT_GROUNDING_DIR
) -> dict[str, Any]:
    servers = build_mcp_servers(env)
    registry_ref = agent_ref(env)
    host_runtime_lane = runtime_lane(env)
    runtime_hint = (
        "You run as a hosted Slack bot on Render without direct shell, file "
        "writes, or browser control. Reach the estate and hosted artifact tools "
        f"through MCP and K2. Your runtime lane is {host_runtime_lane}. Long work "
        "is fine; keep working state in Slack's ephemeral status and send only "
        "the finished answer."
    )
    if registry_ref:
        runtime_hint += (
            f" Your registry identity is {registry_ref}. The host injects one "
            "Katailyst2 wishing-well draw for each substantive turn through its "
            "durable start/get door; judge the live candidates freely and do not "
            "start a duplicate draw. "
            "Your canonical runtime pack is already installed; do not fetch it "
            "again inside a turn. For registry.get, start with card or concise "
            "and load one full body only when the task actually needs it."
        )

    model_provider = (
        _clean(env, "HERMES_INFERENCE_PROVIDER") or DEFAULT_PROVIDER
    ).lower()
    model_name = _clean(env, "HERMES_MODEL")
    if not model_name and model_provider == "openrouter":
        model_name = _clean(env, "OPENROUTER_MODEL")
    model_name = (
        model_name or PROVIDER_DEFAULT_MODELS.get(model_provider) or DEFAULT_MODEL
    )

    config: dict[str, Any] = {
        "_generated_by": GENERATED_BY,
        "model": {
            "provider": model_provider,
            "default": model_name,
            # This is an output ceiling for one provider call, not a context
            # limit. Upstream otherwise advertised the model's 128k maximum to
            # OpenRouter, which pre-authorized a worst-case response and failed
            # a small Slack diagram with HTTP 402 before generation began.
            "max_tokens": DEFAULT_MAX_TOKENS,
        },
        "agent": {
            "max_turns": DEFAULT_MAX_TURNS,
            # Interactive Slack work gets a faster final-summary fail-safe;
            # API/K2 runs keep the full global budget above.
            "platform_max_turns": {"slack": DEFAULT_SLACK_MAX_TURNS},
            # Fixes the "stops after stating intent" failure on some models.
            "intent_ack_continuation": True,
            # Default 180s posts "still working" into a shared channel every
            # three minutes.
            "gateway_notify_interval": 900,
            # Fast failover to the fallback provider rather than slow retries.
            "api_max_retries": 1,
            # High is the pinned Hermes recommendation for Codex-backed Slack:
            # xhigh can consume the turn in hidden thought without visible text.
            # The chosen xAI/OpenAI/OpenRouter routes all support high.
            "reasoning_effort": "high",
            "environment_hint": runtime_hint,
        },
        # Both the identity file and the composed runtime doctrine are allowed
        # a useful slice of a modern long-context model. Upstream otherwise
        # falls back to 20k when model metadata is not resolved at prompt build.
        "context_file_max_chars": 50_000,
        # Bound externally-triggered runs separately from Slack work. This is
        # the exact path the pinned API adapter reads; placing the cap in the
        # platform's `extra` mapping looks plausible but is ignored upstream.
        "gateway": {"api_server": {"max_concurrent_runs": 2}},
        # Keep the global edit transport available for non-Slack gateway
        # surfaces. Slack opts out below so model text before tool calls can
        # never become permanent transcript fragments.
        "streaming": {
            "enabled": True,
            "transport": "edit",
            "edit_interval": 1.0,
            "buffer_threshold": 40,
            "cursor": " ▉",
        },
        "display": {
            # Codex-backed models narrate phase=commentary through a separate
            # agent-level gate (agent_init reads this from the TOP-LEVEL display
            # section only — a per-platform copy is silently ignored). Without
            # this, narration returns the moment the ladder fails over to Codex.
            "show_commentary": False,
            "platforms": {
                "slack": {
                    # Keep every pre-tool assistant segment out of Slack. The
                    # ephemeral working status still shows that work is
                    # active; only the finished response becomes a message.
                    "streaming": False,
                    # Pinned Hermes supports full|verb|off here. `verb` keeps
                    # a useful live cue without leaking arguments, filenames,
                    # commands, or other tool detail into a shared channel.
                    "live_status": "verb",
                    "tool_progress": "off",
                    "interim_assistant_messages": False,
                    "long_running_notifications": False,
                    "busy_ack_detail": False,
                    "show_reasoning": False,
                }
            }
        },
        # Project-context discovery reads AGENTS.md from here.
        "terminal": {"cwd": grounding_dir},
        # A scheduled run has nobody at the keyboard to approve anything, so it
        # must not be able to ask. Upstream already defaults to deny; pinning it
        # means a future default flip cannot quietly hand an unattended job the
        # ability to approve its own dangerous call.
        "approvals": {"cron_mode": "deny"},
        # Upstream default is `edge`, and having ELEVENLABS_API_KEY set is
        # deliberately NOT enough — "Inference credentials do not imply consent
        # to paid speech generation" (tools/tts_tool.py). So the tool gate
        # `check_tts_requirements` returned False and text_to_speech was
        # unavailable every turn while /health cheerfully reported the key
        # present. Naming the provider is the opt-in.
        "tts": {"provider": "elevenlabs"},
        # Per-tool provider selection, which is the documented surface — see
        # upstream's Nous Tool Gateway: `tools.web_search.provider` /
        # `tools.image_generation.provider`, with `use_gateway` routing a call
        # through a Portal subscription instead of our own key.
        #
        # `check_web_api_key` needs a backend that actually resolves; with none
        # configured the ENTIRE `web` toolset is unavailable and she cannot
        # search at all. Firecrawl is a first-class backend and we hold a key,
        # so it is the default here — `ddgs` remains the keyless fallback for a
        # deploy with no Firecrawl credit.
        "tools": {
            "tool_search": {
                "enabled": "auto",
                "search_default_limit": DEFAULT_TOOL_SEARCH_LIMIT,
                "max_search_limit": MAX_TOOL_SEARCH_LIMIT,
                "listing": "auto",
                "listing_max_tokens": TOOL_LISTING_MAX_TOKENS,
            },
            "web_search": {
                "provider": _clean(env, "WEB_SEARCH_BACKEND")
                or ("firecrawl" if _clean(env, "FIRECRAWL_API_KEY") else "ddgs")
            },
            "image_generation": {"provider": "fal"},
        },
        "web": {
            "backend": _clean(env, "WEB_SEARCH_BACKEND")
            or ("firecrawl" if _clean(env, "FIRECRAWL_API_KEY") else "ddgs")
        },
        # The top-level `toolsets` key is deprecated and ignored upstream; this
        # per-platform map is the one that is actually read.
        "platform_toolsets": {
            "slack": slack_toolsets(servers),
            **(
                {"api_server": api_server_toolsets(servers)}
                if _clean(env, "OPENCLAW_HQ_HOOK_TOKEN")
                else {}
            ),
        },
        # Per-surface prompt guidance. Top-level key, NOT under `platforms` —
        # a third Slack namespace, and putting it in the wrong one is silently
        # ignored like every other misplaced Slack key in this file.
        "platform_hints": {"slack": {"append": SLACK_PLATFORM_HINT}},
        "slack": build_slack(env),
        "platforms": build_platforms(env),
        "memory": {
            "memory_enabled": True,
            # USER.md is singular — "what the agent knows about the user". With
            # a whole workspace talking to one agent makes that profile thrash.
            "user_profile_enabled": False,
            # Otherwise one person's thread writes durable facts for everyone.
            "write_approval": True,
        },
        "privacy": {"redact_pii": True},
        "security": {"allow_lazy_installs": False},
        # Managed fleet learning belongs in K2. Upstream's automatic background
        # reviewer replayed the just-finished 57K-78K-token Slack session through
        # five additional model calls, then could not write the user-owned skill
        # it proposed. Disabling that hidden replay does not remove any user tool
        # or interactive capability.
        "auxiliary": {"background_review": {"enabled": False}},
        # Large MCP payloads remain fully recoverable on the durable agent disk;
        # only the active-context preview is bounded here.
        "tool_budget": {
            "mcp_result_size_chars": DEFAULT_MCP_RESULT_SIZE_CHARS,
        },
        # Alec's single-user state.db is already 103 MB; this box is shared.
        "sessions": {"auto_prune": True},
        "session_reset": {"mode": "both", "idle_minutes": 1440},
        # Nothing pinned but the system prompt, rolling summary and recent tail
        # — right for long-lived Slack threads.
        "compression": {
            "enabled": True,
            "threshold_tokens": DEFAULT_COMPRESSION_THRESHOLD_TOKENS,
            "protect_first_n": 0,
            # Routine compaction is internal work, not a teammate-facing
            # message. Hermes v0.21 suppresses it by default; pin the contract
            # so a future default cannot reintroduce "compaction complete".
            "progress_notices": False,
        },
        "prompt_caching": {"cache_ttl": "1h"},
        # Scheduled briefs land in their own thread and stay continuable.
        "cron": {"mirror_delivery": True},
        # The curator archives unused skills after 90 days. Ours are shipped in
        # the image and are meant to persist.
        "curator": {"prune_builtins": False},
        # A user plugin is the pinned runtime's supported turn-context seam.
        # It draws one bounded K2 packet and injects it ephemerally per mission;
        # no Hermes fork and no Slack transport interception are involved.
        "plugins": {"enabled": ["hlt-k2-context"]},
    }

    fallback = fallback_providers(
        env, primary_provider=model_provider, primary_model=model_name
    )
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
    slack_display = config["display"]["platforms"]["slack"]
    summary: dict[str, Any] = {
        "config_path": str(path),
        "model": config["model"]["default"],
        "model_provider": config["model"]["provider"],
        "configured_model_route": [
            {
                "provider": config["model"]["provider"],
                "model": config["model"]["default"],
                "role": "primary",
            },
            *[
                {**route, "role": f"fallback-{index}"}
                for index, route in enumerate(
                    config.get("fallback_providers", []), start=1
                )
            ],
        ],
        "max_tokens": config["model"]["max_tokens"],
        "max_turns": config["agent"]["max_turns"],
        "slack_max_turns": config["agent"]["platform_max_turns"]["slack"],
        "slack_tool_round_limit": DEFAULT_SLACK_TOOL_ROUNDS,
        "compression_threshold_tokens": config["compression"]["threshold_tokens"],
        "mcp_result_size_chars": config["tool_budget"]["mcp_result_size_chars"],
        "tool_search": {
            "default_limit": config["tools"]["tool_search"]["search_default_limit"],
            "max_limit": config["tools"]["tool_search"]["max_search_limit"],
            "listing_max_tokens": config["tools"]["tool_search"]["listing_max_tokens"],
        },
        "background_review_enabled": config["auxiliary"]["background_review"][
            "enabled"
        ],
        "agent_ref": agent_ref(env) or "",
        "runtime_lane": runtime_lane(env),
        "deploy_commit": _clean(env, "RENDER_GIT_COMMIT") or "",
        "hermes_upstream_ref": _clean(env, "HERMES_UPSTREAM_REF") or "",
        "openrouter_key_present": bool(_clean(env, "OPENROUTER_API_KEY")),
        "web_search_backend": config.get("tools", {})
        .get("web_search", {})
        .get("provider", ""),
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
        # The cron briefs deliver here; reported so /health shows an unset
        # home channel rather than leaving the briefs quietly unseeded.
        "home_channel_id": (build_home_channel(env) or {}).get("chat_id", ""),
        "slack_presentation": {
            "one_message_stream": (
                slack_display["streaming"] is False
                and slack_display["interim_assistant_messages"] is False
            ),
            "transport": "final_send",
            "tool_progress": slack_display["tool_progress"],
            "live_status": slack_display["live_status"],
            # The ephemeral working-status line ("is digging through the
            # estate…") keeps a long turn alive without a permanent post.
            "assistant_status": bool(
                config.get("platforms", {}).get("slack", {}).get("typing_indicator")
            ),
            # Codex phase=commentary narration gate — read by agent_init from
            # the TOP-LEVEL display section only.
            "show_commentary": config.get("display", {}).get("show_commentary"),
        },
        "k2_context_plugin": {
            "enabled": "hlt-k2-context" in config.get("plugins", {}).get("enabled", []),
        },
        "external_dispatch": {
            "configured": bool(_clean(env, "OPENCLAW_HQ_HOOK_TOKEN")),
            "public_path": "/hooks/agent",
            "status_path": "/hooks/agent/runs/{runId}",
            "activation_path": "/activationz",
            "readiness_path": "/readyz",
            "hermes_loopback": "http://127.0.0.1:8642/v1/runs",
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
        # The values above describe the config we intended to write. Once an
        # operator-owned file wins, they are no longer proof of what Hermes
        # will load, so readiness must fail closed rather than assume them.
        summary["k2_context_plugin"]["enabled"] = False
        summary["external_dispatch"]["configured"] = False
        return summary

    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    summary["written"] = True
    return summary


if __name__ == "__main__":
    for key, value in render().items():
        print(f"[agent] config {key}: {value}")
