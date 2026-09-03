#!/usr/bin/env python3
"""Validate Cleo's checked-in Slack manifest against the runtime she ships.

Slack can validate the generic manifest schema through ``apps.manifest.validate``.
This local gate covers the equally important cross-file contract: the app must
advertise only the Agent View lifecycle, prompts, commands, and messaging paths
that the pinned Hermes build actually implements.

It is intentionally read-only. Applying or exporting an installed manifest
requires a short-lived Slack app-configuration token and remains a separate
operator step.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

SERVICE_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = SERVICE_DIR / "slack-app-manifest.yaml"

SUPPORTED_EVENTS = {
    "app_mention",
    "app_context_changed",
    "app_home_opened",
    "message.channels",
    "message.groups",
    "message.im",
    "message.mpim",
}
REQUIRED_BOT_SCOPES = {
    "app_mentions:read",
    "assistant:write",
    "channels:history",
    "chat:write",
    "chat:write.customize",
    "commands",
    "groups:history",
    "im:history",
    "im:read",
    "im:write",
    "mpim:history",
    "mpim:read",
    "users:read",
}


def _load_render_config() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hlt_agent_render_config_for_manifest",
        SERVICE_DIR / "render_config.py",
    )
    if spec is None or spec.loader is None:  # pragma: no cover - importlib guard
        raise RuntimeError("could not load services/agent/render_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, str) else []


def validate_manifest(
    manifest: Mapping[str, Any],
    *,
    agent_actions: Sequence[Mapping[str, str]] | None = None,
    suggested_prompts: Sequence[Mapping[str, str]] | None = None,
    user_commands: Sequence[str] | None = None,
) -> list[str]:
    """Return every source/runtime contract violation in ``manifest``."""

    if agent_actions is None or suggested_prompts is None or user_commands is None:
        runtime = _load_render_config()
        if agent_actions is None:
            agent_actions = runtime.AGENT_VIEW_ACTIONS
        if suggested_prompts is None:
            suggested_prompts = runtime.SUGGESTED_PROMPTS
        if user_commands is None:
            user_commands = runtime.USER_ALLOWED_COMMANDS

    errors: list[str] = []
    metadata = _mapping(manifest.get("_metadata"))
    if metadata != {"major_version": 1, "minor_version": 1}:
        errors.append("_metadata must pin Slack manifest schema 1.1")

    display = _mapping(manifest.get("display_information"))
    if display.get("name") != "Cleo":
        errors.append("display_information.name must be Cleo")
    if not str(display.get("description", "")).strip():
        errors.append("display_information.description must be present")
    if not str(display.get("long_description", "")).strip():
        errors.append("display_information.long_description must be present")

    features = _mapping(manifest.get("features"))
    if "assistant_view" in features:
        errors.append("assistant_view is legacy; Cleo must use agent_view")
    agent_view = _mapping(features.get("agent_view"))
    description = str(agent_view.get("agent_description", ""))
    if not description or len(description) > 300:
        errors.append("agent_view.agent_description must be 1-300 characters")

    actions = _list(agent_view.get("actions"))
    if actions != [dict(action) for action in agent_actions]:
        errors.append("agent_view.actions must match render_config.AGENT_VIEW_ACTIONS")

    prompts = _list(agent_view.get("suggested_prompts"))
    if prompts != [dict(prompt) for prompt in suggested_prompts]:
        errors.append("agent_view.suggested_prompts must match render_config.SUGGESTED_PROMPTS")

    bot_user = _mapping(features.get("bot_user"))
    if bot_user.get("display_name") != "Cleo":
        errors.append("features.bot_user.display_name must be Cleo")

    app_home = _mapping(features.get("app_home"))
    expected_app_home = {
        "home_tab_enabled": False,
        "messages_tab_enabled": True,
        "messages_tab_read_only_enabled": False,
    }
    if app_home != expected_app_home:
        errors.append(
            "app_home must keep the implemented Messages tab writable and "
            "the unimplemented Home tab off"
        )

    command_entries = _list(features.get("slash_commands"))
    commands = {
        str(_mapping(entry).get("command", "")) for entry in command_entries
    }
    expected_commands = {f"/{command}" for command in user_commands} | {
        "/approve",
        "/deny",
    }
    if commands != expected_commands or len(command_entries) != len(expected_commands):
        errors.append("slash_commands must match the Hermes user/admin command contract")

    settings = _mapping(manifest.get("settings"))
    event_subscriptions = _mapping(settings.get("event_subscriptions"))
    events = set(_list(event_subscriptions.get("bot_events")))
    if not SUPPORTED_EVENTS <= events:
        missing = ", ".join(sorted(SUPPORTED_EVENTS - events))
        errors.append(f"bot_events is missing implemented human/Agent View events: {missing}")
    outside_contract = events - SUPPORTED_EVENTS
    if outside_contract:
        errors.append(
            "bot_events advertises events outside the implemented contract: "
            + ", ".join(sorted(outside_contract))
        )
    if settings.get("socket_mode_enabled") is not True:
        errors.append("settings.socket_mode_enabled must be true")
    if _mapping(settings.get("interactivity")).get("is_enabled") is not True:
        errors.append("settings.interactivity.is_enabled must be true")

    oauth = _mapping(manifest.get("oauth_config"))
    scopes = _mapping(oauth.get("scopes"))
    bot_scopes = set(_list(scopes.get("bot")))
    if not REQUIRED_BOT_SCOPES <= bot_scopes:
        missing = ", ".join(sorted(REQUIRED_BOT_SCOPES - bot_scopes))
        errors.append(f"bot scopes are missing implemented messaging access: {missing}")

    return errors


def validate_manifest_file(path: Path = DEFAULT_MANIFEST) -> list[str]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        return [f"manifest YAML is invalid: {error}"]
    except OSError as error:
        return [f"manifest could not be read: {error}"]
    if not isinstance(payload, Mapping):
        return ["manifest root must be a mapping"]
    return validate_manifest(payload)


def main() -> int:
    errors = validate_manifest_file()
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Cleo Slack manifest: source/runtime contract valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
