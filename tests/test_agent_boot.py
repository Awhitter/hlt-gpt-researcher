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
    granted = config["platform_toolsets"]["slack"]

    # Every pinned toolset is granted, plus one `mcp-<server>` per mounted
    # server — that suffix is not decoration, it is what actually hands the
    # agent her Linear and codegraph tools.
    assert granted == render_config.slack_toolsets(config["mcp_servers"])
    for toolset in render_config.SLACK_TOOLSETS:
        assert toolset in granted


def test_brian_can_still_do_his_job():
    """Locking down must not remove research capability.

    `search` is gone on purpose — it is web_search alone and `web` already
    bundles web_search + web_extract.
    """
    for tool in ("web", "memory", "skills", "clarify"):
        assert tool in render_config.SLACK_TOOLSETS


def test_privileged_slash_commands_are_not_handed_to_everyone():
    config = render_config.build_config({**FULL_ENV, "SLACK_ADMIN_USERS": "U1,U2"})
    extra = config["platforms"]["slack"]["extra"]

    assert extra["allow_admin_from"] == ["U1", "U2"]
    for dangerous in ("model", "yolo", "rollback", "update", "restart"):
        assert dangerous not in extra["user_allowed_commands"]


# The 50 real Hermes gateway commands, from `hermes slack manifest`. A name in
# user_allowed_commands that is not one of these is silently inert — a teammate
# gets refused something you believe you granted. "status" was exactly that.
REAL_HERMES_COMMANDS = {
    "hermes", "btw", "bg", "start", "new", "retry", "undo", "title", "branch",
    "compress", "rollback", "stop", "approve", "deny", "background", "agents",
    "queue", "steer", "goal", "subgoal", "whoami", "profile", "sethome",
    "resume", "sessions", "model", "codex-runtime", "personality", "footer",
    "yolo", "reasoning", "fast", "voice", "memory", "bundles", "learn",
    "suggestions", "blueprint", "curator", "kanban", "reload-mcp",
    "reload-skills", "commands", "help", "restart", "usage", "insights",
    "platform", "update", "version",
}


def test_every_allowed_command_actually_exists():
    unknown = set(render_config.USER_ALLOWED_COMMANDS) - REAL_HERMES_COMMANDS
    assert not unknown, f"not real Hermes commands, so silently inert: {unknown}"


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
    assert sorted(summary["mcp_unconfigured"]) == ["codegraph", "katailyst2", "posthog"]
    assert summary["openrouter_key_present"] is False
    # The reported toolset is what Hermes was handed, so a mounted server's
    # grant shows up here rather than being invisible.
    assert summary["slack_toolsets"] == (
        list(render_config.SLACK_TOOLSETS) + ["mcp-gpt-researcher", "mcp-linear"]
    )


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


# --- the gateway child's own logging ----------------------------------------


def _cron_seed():
    return _load("cron_seed", SERVICE_DIR / "cron_seed.py")


def _load_health_gateway():
    """Load health_gateway.py without putting the service dir on sys.path.

    It does bare ``import grounding`` / ``import render_config``, which normally
    needs sys.path. Pre-seeding sys.modules under those names resolves them from
    the copies this module already loaded, so the import works and nothing leaks
    into the modules collected after this one.
    """
    import sys

    saved = {
        name: sys.modules.get(name)
        for name in ("grounding", "render_config", "cron_seed")
    }
    sys.modules["grounding"] = grounding
    sys.modules["render_config"] = render_config
    sys.modules["cron_seed"] = _cron_seed()
    try:
        return _load("hlt_agent_health_gateway", SERVICE_DIR / "health_gateway.py")
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


# `run` is not decoration: -v/-q/--external-supervisor live on the `run`
# sub-subparser, so `hermes gateway -v` is an argparse error that crash-loops
# the child. The Dockerfile asserts the real parser still accepts these.
BASE_ARGV = ["hermes", "gateway", "run", "--external-supervisor"]


def test_the_verbosity_flag_goes_where_upstream_declares_it():
    """`hermes gateway -v` is an argparse error, not a verbose gateway.

    Shipping that put the container in a restart loop: argparse printed usage,
    exited non-zero, and the supervisor just saw a child that would not stay
    up. Bare `hermes gateway` and `hermes gateway run` take the same code path
    upstream, so naming `run` costs nothing and makes the flags legal.
    """
    cmd = _load_health_gateway().gateway_command()

    assert cmd[:3] == ["hermes", "gateway", "run"]
    assert cmd.index("run") < cmd.index("-v"), "flags must follow the run subcommand"


def test_the_gateway_child_logs_at_info_by_default():
    """Without -v Hermes prints WARNING and above, and nothing else.

    "Connecting to slack...", the connected confirmation, per-message dispatch
    and authorization denials are all INFO. Run quiet and a bot that is
    connected to nothing produces a log stream indistinguishable from a healthy
    one — which is exactly what happened here: the adapter was present, the
    process was alive, /health was green, and no Slack event ever arrived with
    not one line to say so.
    """
    assert _load_health_gateway().gateway_command() == BASE_ARGV + ["-v"]


def test_verbosity_is_operator_tunable():
    health_gateway = _load_health_gateway()

    assert health_gateway.gateway_command("0") == BASE_ARGV
    assert health_gateway.gateway_command("2") == BASE_ARGV + ["-vv"]
    # argparse counts repeats; -vvv is already DEBUG, so more adds nothing.
    assert health_gateway.gateway_command("9") == BASE_ARGV + ["-vvv"]


def test_a_junk_verbosity_still_boots_the_gateway():
    """A typo in an env var must not take the bot down or silence it."""
    health_gateway = _load_health_gateway()

    assert health_gateway.gateway_command("loud") == BASE_ARGV + ["-v"]
    assert health_gateway.gateway_command("") == BASE_ARGV + ["-v"]


# --- the model credential ---------------------------------------------------


def _load_health_gateway():
    """Load health_gateway.py without putting the service dir on sys.path.

    It does bare ``import grounding`` / ``import render_config``, which normally
    needs sys.path. Pre-seeding sys.modules under those names resolves them from
    the copies this module already loaded, so the import works and nothing leaks
    into the modules collected after this one.
    """
    import sys

    saved = {
        name: sys.modules.get(name)
        for name in ("grounding", "render_config", "cron_seed")
    }
    sys.modules["grounding"] = grounding
    sys.modules["render_config"] = render_config
    sys.modules["cron_seed"] = _cron_seed()
    try:
        return _load("hlt_agent_health_gateway", SERVICE_DIR / "health_gateway.py")
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def _fake_openrouter(monkeypatch, *, payload=None, http_status=None, boom=False):
    """Stand in for OpenRouter's /api/v1/key without touching the network."""
    import io
    import json as _json
    import urllib.error
    import urllib.request

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(request, timeout=None):
        if boom:
            raise OSError("dns went away")
        if http_status is not None:
            raise urllib.error.HTTPError(
                "https://openrouter.ai/api/v1/key", http_status, "nope", {}, None
            )
        return _Response(_json.dumps({"data": payload or {}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)


def test_a_provisioning_key_is_not_a_working_credential(monkeypatch):
    """OpenRouter's two key kinds are indistinguishable by shape.

    A provisioning key authenticates fine and is then refused for every
    completion with 401 "User not found." Cleo shipped on one and answered
    every message "Provider authentication failed", while /health reported
    openrouter_key_present: true — present, and useless.
    """
    health_gateway = _load_health_gateway()
    _fake_openrouter(
        monkeypatch, payload={"is_provisioning_key": True, "is_management_key": True}
    )

    assert health_gateway.openrouter_key_kind("sk-or-v1-whatever") == "provisioning"


def test_a_real_inference_key_reads_as_usable(monkeypatch):
    health_gateway = _load_health_gateway()
    _fake_openrouter(
        monkeypatch, payload={"is_provisioning_key": False, "is_management_key": False}
    )

    assert health_gateway.openrouter_key_kind("sk-or-v1-whatever") == "inference"


def test_a_rejected_key_is_reported_as_rejected(monkeypatch):
    health_gateway = _load_health_gateway()
    _fake_openrouter(monkeypatch, http_status=401)

    assert health_gateway.openrouter_key_kind("sk-or-v1-whatever") == "rejected"


def test_an_unreachable_check_never_condemns_a_working_key(monkeypatch):
    """A flaky network must not make a healthy agent look broken.

    "unknown" is the only honest answer when the check itself could not run,
    and /health treats it as such — degrade only on a positively-identified
    bad key.
    """
    health_gateway = _load_health_gateway()
    _fake_openrouter(monkeypatch, boom=True)

    assert health_gateway.openrouter_key_kind("sk-or-v1-whatever") == "unknown"
    assert health_gateway.openrouter_key_kind("") == "unknown"


def test_mcp_tools_are_actually_granted_to_slack():
    """Mounting an MCP server does not give the agent its tools.

    `platform_toolsets` is an ALLOWLIST and every MCP server registers under a
    dynamic toolset named `mcp-<server>`. Cleo shipped with all four servers
    mounted and none of them listed, so /health reported
    `mcp_mounted: [codegraph, gpt-researcher, katailyst2, linear]` and
    `mcp_without_token: []` — both true — while she had not one of their tools
    and told a teammate they were "not actually available to me in this
    session".
    """
    config = render_config.build_config(FULL_ENV)
    granted = config["platform_toolsets"]["slack"]

    for name in ("codegraph", "gpt-researcher", "katailyst2", "linear"):
        assert name in config["mcp_servers"], f"{name} should be mounted"
        assert f"mcp-{name}" in granted, f"{name} is mounted but its tools are not granted"


def test_an_unconfigured_server_is_not_granted():
    """The grant list is derived from what is mounted, so it cannot drift."""
    env = {k: v for k, v in FULL_ENV.items() if not k.startswith("LINEAR_")}
    config = render_config.build_config(env)

    assert "linear" not in config["mcp_servers"]
    assert "mcp-linear" not in config["platform_toolsets"]["slack"]


def test_media_backends_are_reported(tmp_path):
    """The image/audio tools load without their key and fail at call time."""
    summary = render_config.render(env=FULL_ENV, home=tmp_path)
    assert summary["media_backends"] == {"image_generate": False, "text_to_speech": False}

    with_keys = render_config.render(
        env={**FULL_ENV, "FAL_KEY": "fal-x", "ELEVENLABS_API_KEY": "el-x"}, home=tmp_path
    )
    assert with_keys["media_backends"] == {"image_generate": True, "text_to_speech": True}


def test_slack_gets_its_own_prompt_guidance():
    config = render_config.build_config(FULL_ENV)
    hint = config["platform_hints"]["slack"]["append"]

    assert "did NOT build this system" in hint
    # Evidence goes at the END as a Sources list; scattering ids mid-sentence
    # is what made her answers read like machine-room tours.
    assert "Sources list" in hint, "grounding an answer in a real source is the point"


# --- talking to the team, not about the plumbing ----------------------------


def test_the_setup_nag_is_switched_off():
    """Hermes greets every new session with a "No home channel" notice.

    It tells the user to type `/hermes sethome` — a command our manifest did
    not declare, so it fails. That notice was the first thing a new teammate
    ever saw from this agent. Setting the home channel removes it for everyone
    without anyone running anything.
    """
    slack = render_config.build_platforms({"SLACK_HOME_CHANNEL": "C0BN349TRU7|#cleo"})["slack"]

    assert slack["home_channel"] == {
        "platform": "slack",
        "chat_id": "C0BN349TRU7",
        "name": "#cleo",
    }


def test_a_malformed_home_channel_is_omitted_not_written():
    """A half-formed home channel silently breaks cron delivery."""
    assert "home_channel" not in render_config.build_platforms({})["slack"]
    assert render_config.build_home_channel({"SLACK_HOME_CHANNEL": "|#cleo"}) is None
    assert render_config.build_home_channel({"SLACK_HOME_CHANNEL": "  "}) is None
    # id alone is fine — the label is cosmetic
    assert render_config.build_home_channel({"SLACK_HOME_CHANNEL": "C123"})["name"] == "C123"


def test_posthog_is_mounted_and_granted():
    """Marketing asks "is it working", which needs numbers, not architecture."""
    env = {**FULL_ENV, "POSTHOG_MCP_URL": "https://mcp.posthog.com/mcp", "POSTHOG_MCP_TOKEN": "phx"}
    config = render_config.build_config(env)

    assert "posthog" in config["mcp_servers"]
    assert "mcp-posthog" in config["platform_toolsets"]["slack"]


def test_the_briefing_carries_the_business_not_just_the_code(tmp_path):
    """A marketing lead got an answer about `proxy.ts` because the briefing only
    described the codebase.

    Team context is deliberately NOT duplicated here — `global-team-context` in
    the Katailyst registry owns it fleet-wide, and a local copy is a second
    canon that drifts.
    """
    summary = grounding.install(agent="cleo", home=tmp_path, env={})

    assert summary["briefing_sections"] == ["shared", "cleo"]
    briefing = (tmp_path / "grounding" / "AGENTS.md").read_text(encoding="utf-8")
    assert "NCLEX RN Mastery is the mass surface" in briefing
    assert "Cedar Rapids" in briefing, "a tried-and-failed campaign must not be re-proposed"


def test_slack_hint_forbids_leading_with_internals():
    hint = render_config.build_config(FULL_ENV)["platform_hints"]["slack"]["append"]

    assert "register of the person asking" in hint
    assert "Query the source before you describe it" in hint


def test_mounted_servers_without_the_mcp_sdk_are_reported_dead():
    """`mcp` is an optional upstream extra, and without it Hermes disables MCP
    entirely at DEBUG while every server still reads as mounted and granted.

    That is how Cleo came to tell a teammate her Linear and codegraph tools were
    "not actually available to me in this session" with /health showing four
    servers mounted and no missing tokens.
    """
    health_gateway = _load_health_gateway()
    supervisor = health_gateway.GatewaySupervisor()

    # The property is what /health reads; it must reflect the INSTALLED package,
    # never a config value — that distinction is the whole point.
    import importlib.util as _iu

    assert supervisor.mcp_sdk_available == (_iu.find_spec("mcp") is not None)
    assert "mcp_sdk_available" in supervisor.snapshot()


def test_paid_speech_needs_an_explicit_provider():
    """`check_tts_requirements` resolves the CONFIGURED provider, and upstream
    defaults to `edge` — a key alone leaves text_to_speech gated off every turn
    ("Inference credentials do not imply consent to paid speech generation").
    """
    assert render_config.build_config(FULL_ENV)["tts"]["provider"] == "elevenlabs"


def test_web_search_has_a_backend_that_needs_no_key():
    """With no backend configured `check_web_api_key` is False and the whole
    `web` toolset is unavailable — she cannot search at all."""
    assert render_config.build_config(FULL_ENV)["web"]["backend"] == "ddgs"
    override = render_config.build_config({**FULL_ENV, "WEB_SEARCH_BACKEND": "tavily"})
    assert override["web"]["backend"] == "tavily"


def test_an_orientation_answer_must_carry_recent_change():
    """"help me understand nursing mastery" returned a static architecture tour
    while sign-in and data ownership had just moved (#712, #556).

    The briefing routed orientation questions to structure only — "code graph
    for how" — so last week never appeared. A description of a system that
    merges hundreds of PRs a fortnight, minus what just changed, is a stale
    snapshot the reader acts on.
    """
    soul = (SERVICE_DIR / "grounding" / "cleo" / "SOUL.md").read_text(encoding="utf-8")
    assert "orientation question is never answered from structure alone" in soul
    assert "recent_changes" in soul

    hint = render_config.build_config(FULL_ENV)["platform_hints"]["slack"]["append"]
    assert "orientation question" in hint
    assert "LEAD with what" in hint


def test_capabilities_use_the_documented_provider_surface():
    """`tools.<tool>.provider` is upstream's Tool Gateway surface. A missing
    backend leaves check_web_api_key False and the whole web toolset dead."""
    with_key = render_config.build_config({**FULL_ENV, "FIRECRAWL_API_KEY": "fc-x"})
    assert with_key["tools"]["web_search"]["provider"] == "firecrawl"
    assert with_key["tools"]["image_generation"]["provider"] == "fal"

    # keyless deploys must still resolve a backend, or web search silently dies
    assert render_config.build_config(FULL_ENV)["tools"]["web_search"]["provider"] == "ddgs"


def test_recent_change_is_ranked_by_consequence_not_visibility():
    """She pulled recent_changes correctly and still buried the important part.

    Asked to explain Nursing Mastery she led with a ranked job board, an
    onboarding location change and a mobile pass — while `#556 feat(auth): sign
    in gets its front door` (2026-07-30, inside her window) and "a backend step
    can no longer sink her save" (which would have failed EVERY save on
    /onboarding, /start/quick and /start/nclex) went unmentioned.

    Visible does not mean consequential. Where truth lives moving outranks any
    feature.
    """
    soul = (SERVICE_DIR / "grounding" / "cleo" / "SOUL.md").read_text(encoding="utf-8")
    assert "Rank what changed by consequence, not by visibility" in soul
    assert "Where truth lives moved" in soul

    hint = render_config.build_config(FULL_ENV)["platform_hints"]["slack"]["append"]
    assert "CONSEQUENCE, not visibility" in hint


def test_skills_are_installed_where_hermes_reads_them(tmp_path):
    """She shipped with the `skills` toolset granted and zero skills installed.

    Hermes scans a "Skills (mandatory)" index before every reply; an empty
    directory means that scan finds nothing, every turn. Skills also keep the
    always-on briefing small — procedure belongs in a skill, not in AGENTS.md,
    which is capped by context_file_max_chars.
    """
    summary = grounding.install(agent="cleo", home=tmp_path, env={})

    assert "orient-a-newcomer" in summary["skills_installed"]
    body = (tmp_path / "skills" / "orient-a-newcomer" / "SKILL.md").read_text(encoding="utf-8")
    assert "recent_changes(repo, days=14)" in body
    assert "Rank what changed by consequence" in body or "by consequence" in body


def test_a_hand_edited_skill_is_left_alone(tmp_path):
    """Same courtesy as SOUL.md: boot refreshes its own files, never yours."""
    grounding.install(agent="cleo", home=tmp_path, env={})
    edited = tmp_path / "skills" / "weekly-brief" / "SKILL.md"
    edited.write_text("# my own version\n", encoding="utf-8")

    summary = grounding.install(agent="cleo", home=tmp_path, env={})

    assert edited.read_text(encoding="utf-8") == "# my own version\n"
    assert "weekly-brief" not in summary["skills_installed"]


def test_an_orientation_asks_for_a_fortnight():
    """She narrowed to three days and missed a sign-in change eight days back."""
    soul = (SERVICE_DIR / "grounding" / "cleo" / "SOUL.md").read_text(encoding="utf-8")
    assert "fortnight at least" in soul
    assert "complete: false" in soul, "she must report the window she truly covered"


# --- recurring briefs -------------------------------------------------------


def test_a_brief_delivers_to_the_home_channel_not_to_origin(tmp_path, monkeypatch):
    """`--deliver origin` would post a scheduled brief into whatever session ran
    last. The home channel is the only target that means the same thing every
    week."""
    cron_seed = _cron_seed()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return __import__("subprocess").CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cron_seed.subprocess, "run", fake_run)
    result = cron_seed.seed("slack:C0BN349TRU7")

    assert result["created"] == ["nm-monday-brief", "nm-board-health"]
    assert result["failed"] == []
    for cmd in calls:
        assert cmd[:3] == ["hermes", "cron", "create"]
        assert "--deliver" in cmd and cmd[cmd.index("--deliver") + 1] == "slack:C0BN349TRU7"
        # Every brief loads its skill, so the procedure has one home.
        assert "--skill" in cmd


def test_a_second_boot_does_not_duplicate_a_brief(tmp_path, monkeypatch):
    """Render redeploys on every merge. A brief seeded per boot would reach the
    team a dozen times a week."""
    cron_seed = _cron_seed()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    jobs = tmp_path / "cron"
    jobs.mkdir()
    (jobs / "jobs.json").write_text(
        '{"jobs": [{"name": "nm-monday-brief"}, {"name": "nm-board-health"}]}',
        encoding="utf-8",
    )

    def explode(cmd, **kwargs):  # pragma: no cover - must never run
        raise AssertionError(f"seeded a duplicate: {cmd}")

    monkeypatch.setattr(cron_seed.subprocess, "run", explode)
    result = cron_seed.seed("slack:C0BN349TRU7")

    assert result["created"] == []
    assert sorted(result["existing"]) == ["nm-board-health", "nm-monday-brief"]


def test_an_unreadable_job_record_seeds_nothing(tmp_path, monkeypatch):
    """A corrupt record is not proof the jobs are absent. Seeding on a failed
    read is how you get the same brief delivered twice."""
    cron_seed = _cron_seed()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "jobs.json").write_text("{not json", encoding="utf-8")

    def explode(cmd, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("seeded despite an unreadable record")

    monkeypatch.setattr(cron_seed.subprocess, "run", explode)
    result = cron_seed.seed("slack:C0BN349TRU7")

    assert result["created"] == []
    assert result["failed"] == ["read-jobs-file"]


def test_no_home_channel_means_no_brief(tmp_path, monkeypatch):
    """A job created with an empty deliver target fails silently every week."""
    cron_seed = _cron_seed()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def explode(cmd, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("created a brief with nowhere to deliver it")

    monkeypatch.setattr(cron_seed.subprocess, "run", explode)
    assert cron_seed.seed("")["created"] == []


def test_a_scheduled_run_cannot_approve_its_own_dangerous_call():
    """Nobody is at the keyboard at 13:00 on a Monday. Upstream defaults
    approvals.cron_mode to deny; pinning it means a future default flip cannot
    quietly hand an unattended job that power."""
    config = render_config.build_config(FULL_ENV)
    assert config["approvals"]["cron_mode"] == "deny"


def test_the_home_channel_id_is_reported_for_the_briefs(tmp_path):
    """/health has to show an unset home channel, or the briefs are simply
    absent and nothing says so."""
    env = {**FULL_ENV, "SLACK_HOME_CHANNEL": "C0BN349TRU7|#cleo"}
    assert render_config.render(env, home=tmp_path)["home_channel_id"] == "C0BN349TRU7"
    # Unset must read as empty, not crash and not look configured.
    assert render_config.render(FULL_ENV, home=tmp_path)["home_channel_id"] == ""


def test_the_home_channel_env_is_normalised_for_the_scheduler(monkeypatch, tmp_path):
    """We write SLACK_HOME_CHANNEL as "C0BN349TRU7|#cleo" and parse it here.
    Hermes' scheduler does not parse it — `_get_home_target_chat_id` returns the
    raw env value — so a job delivering to bare `slack` would post to a chat id
    of "C0BN349TRU7|#cleo". Boot must hand the child a bare id.
    """
    health_gateway = _load_health_gateway()
    monkeypatch.setenv("SLACK_HOME_CHANNEL", "C0BN349TRU7|#cleo")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(health_gateway, "GATEWAY_ENABLED", True)
    monkeypatch.setattr(health_gateway.supervisor, "start", lambda: None)
    monkeypatch.setattr(health_gateway, "openrouter_key_kind", lambda key: "inference")
    monkeypatch.setattr(health_gateway.cron_seed, "seed", lambda deliver: {"deliver": deliver})

    health_gateway.boot()

    import os as _os

    assert _os.environ["SLACK_HOME_CHANNEL"] == "C0BN349TRU7"
    assert health_gateway.BOOT["cron_briefs"]["deliver"] == "slack:C0BN349TRU7"


def test_a_malformed_home_channel_seeds_nothing(tmp_path, monkeypatch):
    """A typo'd SLACK_HOME_CHANNEL would otherwise be baked into a job that
    fails to deliver, silently, every week."""
    cron_seed = _cron_seed()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def explode(cmd, **kwargs):  # pragma: no cover - must never run
        raise AssertionError(f"seeded with a bad target: {cmd}")

    monkeypatch.setattr(cron_seed.subprocess, "run", explode)
    for bad in ("slack:", "slack:#cleo", "slack:C0BN349TRU7|#cleo", "C0BN349TRU7"):
        result = cron_seed.seed(bad)
        assert result["created"] == [], bad
        assert result["failed"] == ["bad-deliver-target"], bad

    # And the real one still works.
    monkeypatch.setattr(
        cron_seed.subprocess, "run",
        lambda cmd, **kw: __import__("subprocess").CompletedProcess(cmd, 0, "", ""),
    )
    assert cron_seed.seed("slack:C0BN349TRU7")["created"]


def test_the_one_shot_proof_runs_once_ever(tmp_path, monkeypatch):
    """A brief that has never fired is not a working brief. But the scheduler
    auto-deletes a finite one-shot once it runs, so name-based idempotency would
    re-create it on every deploy and the channel would get a smoke message every
    merge. The sentinel is what stops that."""
    cron_seed = _cron_seed()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return __import__("subprocess").CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cron_seed.subprocess, "run", fake_run)

    assert cron_seed.seed_smoke("slack:C0BN349TRU7") == "created"
    assert cron_seed.seed_smoke("slack:C0BN349TRU7") == "already-run"
    assert len(calls) == 1
    # Finite one-shot, or it repeats forever every five minutes.
    assert "--repeat" in calls[0] and calls[0][calls[0].index("--repeat") + 1] == "1"


def test_a_failed_proof_is_retried_not_marked_done(tmp_path, monkeypatch):
    """Writing the sentinel before knowing the create succeeded would burn the
    single attempt and leave the pipeline unproven forever."""
    cron_seed = _cron_seed()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        cron_seed.subprocess, "run",
        lambda cmd, **kw: __import__("subprocess").CompletedProcess(cmd, 1, "", "boom"),
    )

    assert cron_seed.seed_smoke("slack:C0BN349TRU7") == "failed"
    assert not (tmp_path / cron_seed.SMOKE_SENTINEL).exists()


def test_the_proof_asks_for_a_real_identifier():
    """'Did cron fire' is the easy half. The half that breaks is whether a cron
    session — a standalone agent on the scheduler's own thread pool, outside the
    gateway's dispatch — has her MCP tools at all."""
    cron_seed = _cron_seed()
    assert "NUR" in cron_seed.SMOKE_PROMPT
    assert "real identifier" in cron_seed.SMOKE_PROMPT
    assert "cannot reach a source" in cron_seed.SMOKE_PROMPT


def test_structure_is_never_sent_to_a_text_to_image_model():
    """She offers "want me to draw how this flows?" in nearly every reply, and
    the only drawing tool behind it is FLUX. A text-to-image model cannot spell
    the labels, so an architecture request comes back handsome and wrong — a
    defect class this estate has already paid for once. Structure goes in a code
    block; image_generate is for something genuinely pictorial.
    """
    hint = render_config.build_config(FULL_ENV)["platform_hints"]["slack"]["append"]
    assert "NEVER send structure to image_generate" in hint
    assert "code block" in hint
    assert "garbled" in hint
    # And she must name the form she CAN give rather than produce a wrong one.
    assert "say which form you can give them" in hint
