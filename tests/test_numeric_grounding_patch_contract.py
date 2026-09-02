"""Pin the HLT overlay that installs the grounding gate into upstream Hermes."""

from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "agent"


def test_docker_build_applies_and_asserts_the_numeric_grounding_overlay():
    dockerfile = (SERVICE_DIR / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY hlt_numeric_grounding.py /app/hlt_numeric_grounding.py" in dockerfile
    check = "apply --check /tmp/hermes-patches/api_runs_numeric_grounding.patch"
    apply = "apply /tmp/hermes-patches/api_runs_numeric_grounding.patch"
    assertion = "PYTHONPATH=/app python /tmp/hermes-patches/assert_api_runs_numeric_grounding.py /opt/hermes"
    assert dockerfile.index(check) < dockerfile.index(apply) < dockerfile.index(assertion)


def test_gateway_child_inherits_the_numeric_grounding_module_path():
    dockerfile = (SERVICE_DIR / "Dockerfile").read_text(encoding="utf-8")

    # The gateway subprocess runs from HERMES_HOME (/data/hermes), so a build-only
    # PYTHONPATH prefix does not make /app/hlt_numeric_grounding.py importable.
    # Pin the image-level environment before Hermes drops privileges and starts.
    runtime_path = "ENV PYTHONPATH=/app"
    assert runtime_path in dockerfile
    assert dockerfile.index(runtime_path) < dockerfile.index("USER hermes")
    assert dockerfile.index(runtime_path) < dockerfile.index('CMD ["/app/entrypoint.sh"]')


def test_overlay_gates_completion_and_closes_the_owned_agent_session():
    patch = (
        SERVICE_DIR / "hermes_patches" / "api_runs_numeric_grounding.patch"
    ).read_text(encoding="utf-8")
    added_lines = "\n".join(
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )

    assert "+from hlt_numeric_grounding import NumericGroundingLedger" in patch
    assert "numeric_grounding.observe_tool_event(" in added_lines
    assert added_lines.index("grounding_verdict =") < added_lines.index(
        '"event": "run.completed"'
    )
    assert '"event": "run.failed"' in added_lines
    assert "agent.close()" in added_lines
    assert "return r, u" in added_lines


def test_cleo_source_carries_the_same_reconciliation_contract():
    cleo = SERVICE_DIR / "grounding" / "cleo"
    agents = (cleo / "AGENTS.md").read_text(encoding="utf-8")
    soul = (cleo / "SOUL.md").read_text(encoding="utf-8")
    skill = (
        cleo / "skills" / "facilitate-product-work" / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "Reconcile exact claims before delivery" in agents
    assert "current request" in agents and "current-run tool result" in agents
    assert "Reconcile numeric evidence" in soul
    assert "Reconcile numeric evidence" in skill
    assert "future target" in skill


def test_docs_pin_the_v1_runs_boundary_without_claiming_slack_coverage():
    agent_readme = (SERVICE_DIR / "README.md").read_text(encoding="utf-8")
    root_agents = (SERVICE_DIR.parents[1] / "AGENTS.md").read_text(encoding="utf-8")

    assert "hosted `/v1/runs`" in agent_readme
    assert "Direct Slack gateway delivery does not traverse this" in agent_readme
    assert "boundary guarantee for K2-hosted runs" in root_agents
    assert "direct Slack gateway delivery does" in root_agents
