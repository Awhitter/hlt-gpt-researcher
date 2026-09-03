"""Focused contract tests for Cleo's source-of-truth Slack manifest."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "agent"


def _load(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract = _load(
    "hlt_slack_manifest_contract",
    SERVICE_DIR / "validate_slack_manifest.py",
)
render_config = _load("hlt_manifest_render_config", SERVICE_DIR / "render_config.py")


def _manifest():
    return yaml.safe_load(
        (SERVICE_DIR / "slack-app-manifest.yaml").read_text(encoding="utf-8")
    )


def _validate(manifest):
    return contract.validate_manifest(
        manifest,
        agent_actions=render_config.AGENT_VIEW_ACTIONS,
        suggested_prompts=render_config.SUGGESTED_PROMPTS,
        user_commands=render_config.USER_ALLOWED_COMMANDS,
    )


def test_canonical_manifest_matches_the_runtime_contract():
    assert _validate(_manifest()) == []


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    [
        (
            lambda value: value.pop("_metadata"),
            "_metadata must pin Slack manifest schema 1.1",
        ),
        (
            lambda value: value["features"]["app_home"].update(
                {"home_tab_enabled": True}
            ),
            "unimplemented Home tab off",
        ),
        (
            lambda value: value["features"]["app_home"].update(
                {"messages_tab_read_only_enabled": True}
            ),
            "Messages tab writable",
        ),
        (
            lambda value: value["settings"]["event_subscriptions"][
                "bot_events"
            ].remove("message.im"),
            "message.im",
        ),
        (
            lambda value: value["settings"]["event_subscriptions"][
                "bot_events"
            ].remove("agent_session_stopped"),
            "agent_session_stopped",
        ),
        (
            lambda value: value["oauth_config"]["scopes"]["bot"].remove(
                "assistant:write"
            ),
            "assistant:write",
        ),
        (
            lambda value: value["features"]["agent_view"][
                "suggested_prompts"
            ].clear(),
            "must match render_config.SUGGESTED_PROMPTS",
        ),
        (
            lambda value: value["features"]["agent_view"]["actions"][0].update(
                {"name": "Arbitrary action"}
            ),
            "must match render_config.AGENT_VIEW_ACTIONS",
        ),
    ],
)
def test_validator_rejects_source_runtime_drift(mutate, expected_error):
    manifest = copy.deepcopy(_manifest())
    mutate(manifest)
    assert expected_error in "\n".join(_validate(manifest))


def test_validator_reports_malformed_yaml_without_a_traceback(tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text("features: [not: valid", encoding="utf-8")
    assert "manifest YAML is invalid" in "\n".join(contract.validate_manifest_file(path))


def test_validator_reports_an_unreadable_manifest_without_a_traceback(tmp_path):
    path = tmp_path / "missing.yaml"
    assert "manifest could not be read" in "\n".join(
        contract.validate_manifest_file(path)
    )
