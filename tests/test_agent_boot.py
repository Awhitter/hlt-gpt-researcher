"""Contract tests for the agent container's boot config.

Three separate mechanisms in this service turned out to be silent no-ops:

* the config Hermes reads was documented but never written,
* `/health` reported "ready" from env-var presence rather than observed state,
* the memory seeder wrote `$HERMES_HOME/memory/*.md` while Hermes reads
  `$HERMES_HOME/memories/MEMORY.md` — wrong directory and wrong filenames.

So these tests pin behaviour, not spelling. The most important ones are the
toolset tests: upstream's default Slack toolset grants terminal and
execute_code to anyone who can @mention the bot, and Brian additionally reads
untrusted web pages.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "agent"

# The interpolation pattern Hermes itself uses (hermes_cli/config.py).
HERMES_ENV_REF = re.compile(r"\$\{([^}]+)\}")


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


render_config = _load("hlt_agent_render_config", SERVICE_DIR / "render_config.py")
grounding = _load("hlt_agent_grounding", SERVICE_DIR / "grounding.py")

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


# --- the security perimeter -------------------------------------------------

# Upstream's `hermes-slack` preset resolves to _HERMES_CORE_TOOLS, which is
# "full access for workspace use". Any of these reaching a Slack-reachable
# research agent is a remote code execution path.
FORBIDDEN_TOOLS = (
    "terminal",
    "process",
    "execute_code",
    "code_execution",
    "computer_use",
    "cronjob",
    "browser",
    "file",  # grants write_file + patch; there is no read-only variant
    "write_file",
    "patch",
    "skill_manage",
)


@pytest.mark.parametrize("tool", FORBIDDEN_TOOLS)
def test_slack_toolset_excludes_host_access(tool):
    assert tool not in render_config.SLACK_TOOLSETS, (
        f"{tool} would give anyone who can @mention Brian host access"
    )


def test_slack_toolset_is_pinned_in_the_config_hermes_reads():
    """The top-level `toolsets` key is deprecated and ignored upstream.

    Only `platform_toolsets` is read (hermes_cli/tools_config.py). Writing the
    wrong one leaves the default full-access preset in force.
    """
    config = render_config.build_config(FULL_ENV)
    assert config["platform_toolsets"]["slack"] == list(render_config.SLACK_TOOLSETS)


def test_brian_can_still_do_his_job():
    """Locking down must not remove research capability."""
    for tool in ("web", "search", "memory", "skills", "clarify"):
        assert tool in render_config.SLACK_TOOLSETS


def test_privileged_slash_commands_are_not_handed_to_everyone():
    config = render_config.build_config({**FULL_ENV, "SLACK_ADMIN_USERS": "U1,U2"})
    extra = config["platforms"]["slack"]["extra"]

    assert extra["allow_admin_from"] == ["U1", "U2"]
    for dangerous in ("model", "yolo", "cron", "reset"):
        assert dangerous not in extra["user_allowed_commands"]


def test_missing_admin_list_is_visible_in_health(tmp_path):
    """Hermes disables slash gating entirely when no admin list is set, so the
    absence has to be reportable rather than silent."""
    summary = render_config.render(env=FULL_ENV, home=tmp_path)
    assert summary["slack_admins_configured"] is False
    assert summary["slack_channel_allowlist"] is False


def test_a_workspace_bot_must_not_deny_the_workspace():
    """Hermes defaults to denying unknown senders on every messaging platform.

    Left at the default the bot is simply silent for everyone, with only a
    startup warning to say so — which is what happened on first boot.
    """
    extra = render_config.build_config(FULL_ENV)["platforms"]["slack"]["extra"]
    assert extra["dm_policy"] == "open"
    assert extra["group_policy"] == "open"


def test_sender_policy_is_reported(tmp_path):
    assert render_config.render(env=FULL_ENV, home=tmp_path)["slack_senders_allowed"] == "none"
    opened = render_config.render(
        env={**FULL_ENV, "GATEWAY_ALLOW_ALL_USERS": "true"}, home=tmp_path
    )
    assert opened["slack_senders_allowed"] == "all"


def test_workspace_safety_defaults():
    config = render_config.build_config(FULL_ENV)

    assert config["slack"]["strict_mention"] is True
    assert config["memory"]["write_approval"] is True
    # USER.md is singular; a whole workspace makes that profile incoherent.
    assert config["memory"]["user_profile_enabled"] is False
    assert config["privacy"]["redact_pii"] is True
    assert config["security"]["allow_lazy_installs"] is False


def test_mcp_servers_cannot_bill_us_for_their_own_llm_calls():
    """Server-initiated sampling is ON by default upstream."""
    for name, server in render_config.build_mcp_servers(FULL_ENV).items():
        assert server["sampling"]["enabled"] is False, f"{name} may self-sample"


# --- mounts and secrets -----------------------------------------------------


def test_mounts_only_configured_servers():
    servers = render_config.build_mcp_servers(
        {"GPTR_MCP_URL": "https://gptr.example/mcp", "GPTR_MCP_TOKEN": "t"}
    )
    assert set(servers) == {"gpt-researcher"}


def test_url_without_token_mounts_without_auth_header():
    servers = render_config.build_mcp_servers({"LINEAR_MCP_URL": "https://l.example/mcp"})
    assert servers["linear"]["url"] == "https://l.example/mcp"
    assert "headers" not in servers["linear"]


def test_empty_string_env_is_treated_as_unset():
    assert render_config.build_mcp_servers({"GPTR_MCP_URL": "  ", "GPTR_MCP_TOKEN": "t"}) == {}


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

    assert config["model"]["provider"] == "openrouter"
    assert set(config["mcp_servers"]) == {"gpt-researcher", "codegraph", "katailyst2", "linear"}
    # `gateway:` and `memory.seed_paths` were in the old example file and are
    # not real Hermes config keys.
    assert "gateway" not in config
    assert "seed_paths" not in config["memory"]


def test_grounding_dir_is_explicit(tmp_path):
    """Left at ".", Hermes' project-context discovery resolves into its own
    install tree and silently loads nothing."""
    config = render_config.build_config(FULL_ENV)
    assert config["terminal"]["cwd"] == render_config.DEFAULT_GROUNDING_DIR
    assert config["terminal"]["cwd"] != "."

    # And a real render points it at the composed briefing on the disk.
    render_config.render(env=FULL_ENV, home=tmp_path)
    written = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert written["terminal"]["cwd"] == str(tmp_path / "grounding")


# --- agent selection --------------------------------------------------------


def test_agent_id_selects_the_persona(tmp_path):
    """One image, two agents. The wrong persona in a Slack workspace is loud."""
    cleo = grounding.install(home=tmp_path / "a", env={"AGENT_ID": "cleo"})
    brian = grounding.install(home=tmp_path / "b", env={"AGENT_ID": "brian"})

    assert cleo["agent"] == "cleo"
    assert brian["agent"] == "brian"
    assert "Cleo" in (tmp_path / "a" / "SOUL.md").read_text(encoding="utf-8")
    assert "Brian" in (tmp_path / "b" / "SOUL.md").read_text(encoding="utf-8")


def test_unknown_agent_id_falls_back_but_says_so(tmp_path):
    summary = grounding.install(home=tmp_path, env={"AGENT_ID": "clio"})

    assert summary["agent"] == grounding.DEFAULT_AGENT
    assert summary["agent_id_unrecognised"] is True, "a typo must not boot silently"


def test_unset_agent_id_is_not_flagged(tmp_path):
    summary = grounding.install(home=tmp_path, env={})
    assert summary["agent"] == grounding.DEFAULT_AGENT
    assert summary["agent_id_unrecognised"] is False


def test_briefing_is_shared_facts_plus_the_agent_s_own(tmp_path):
    grounding.install(home=tmp_path, env={"AGENT_ID": "cleo"})
    briefing = (tmp_path / "grounding" / "AGENTS.md").read_text(encoding="utf-8")

    # Shared estate facts, written once.
    assert "Healthcare Learning Technologies" in briefing
    # Cleo's own section.
    assert "Nursing Mastery has no database" in briefing


def test_every_declared_agent_has_a_soul(tmp_path):
    """A registered agent with no SOUL.md would boot voiceless."""
    for agent in grounding.AGENT_IDS:
        home = tmp_path / agent
        summary = grounding.install(agent=agent, home=home, env={})
        assert summary["soul_installed"] is True, f"{agent} has no SOUL.md"
        assert "shared" in summary["briefing_sections"]


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
    assert summary["slack_toolsets"] == list(render_config.SLACK_TOOLSETS)


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
    home = tmp_path / "nested" / "brian"
    render_config.render(env=FULL_ENV, home=home)
    assert (home / "config.yaml").exists()


# --- grounding --------------------------------------------------------------


def test_soul_is_installed_where_hermes_reads_it(tmp_path):
    summary = grounding.install(agent="cleo", home=tmp_path, env={})

    assert summary["soul_installed"] is True
    # HERMES_HOME/SOUL.md — not memories/, not memory/.
    assert (tmp_path / "SOUL.md").is_file()
    assert "Cleo" in (tmp_path / "SOUL.md").read_text(encoding="utf-8")


def test_a_soul_written_under_the_old_marker_is_still_ours(tmp_path):
    """Renaming the marker must not orphan files already on the persistent disk.

    It did: switching this container from Brian to Cleo left Brian's SOUL.md in
    place, because the new code read the old marker as a hand-edit. The box ran
    live wearing the wrong identity until this was fixed.
    """
    (tmp_path / "SOUL.md").write_text(
        f"{grounding.LEGACY_MARKERS[0]}\n# Brian\n", encoding="utf-8"
    )

    summary = grounding.install(agent="cleo", home=tmp_path, env={})

    assert summary["soul_installed"] is True
    assert summary["soul_preserved_operator_edit"] is False
    assert "Cleo" in (tmp_path / "SOUL.md").read_text(encoding="utf-8")


def test_hand_edited_soul_is_preserved(tmp_path):
    (tmp_path / "SOUL.md").write_text("# my own persona\n", encoding="utf-8")

    summary = grounding.install(agent="cleo", home=tmp_path, env={})

    assert summary["soul_preserved_operator_edit"] is True
    assert summary["soul_installed"] is False
    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "# my own persona\n"


def test_our_own_soul_is_refreshed_on_reboot(tmp_path):
    grounding.install(agent="cleo", home=tmp_path, env={})
    assert grounding.install(agent="cleo", home=tmp_path, env={})["soul_installed"] is True


def test_company_facts_ship_in_the_image_not_in_memory(tmp_path):
    """AGENTS.md is the durable briefing and must be read-only to the agent.

    MEMORY.md is capped at ~2200 chars, frozen per session and agent-writable —
    the wrong container for a company knowledge base.
    """
    summary = grounding.install(agent="cleo", home=tmp_path, env={})

    assert summary["briefing_sections"] == ["shared", "cleo"]
    assert not (tmp_path / "memories").exists(), "boot must not pre-seed agent memory"
    assert not (tmp_path / "memory").exists(), "the old wrong-directory seeding is gone"
