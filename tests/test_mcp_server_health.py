import asyncio

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

import mcp_server.server as mcp_server
from mcp_server.server import app, mcp
from mcp_server.tools import SourcePolicyInput


def test_mcp_health_includes_redacted_langfuse_status():
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    langfuse = body["observability"]["langfuse"]
    assert body["service"] == "gpt-researcher-mcp"
    assert set(langfuse) >= {
        "configured",
        "package_available",
        "public_key",
        "secret_key",
        "base_url",
        "record_io",
    }
    assert "sk-" not in str(body)
    assert "pk-" not in str(body)


def test_make_research_adapter_is_installed_on_the_authenticated_mcp_app():
    paths = {getattr(route, "path", None) for route in app.routes}

    assert "/automation/research/v1" in paths
    assert "/automation/research/jobs/v1/start" in paths
    assert "/automation/research/jobs/v1/{request_id}/status" in paths
    assert "/automation/research/jobs/v1/{request_id}/result" in paths


def test_mcp_lifespan_schedules_durable_automation_recovery(monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        mcp_server,
        "schedule_automation_research_recovery",
        lambda: scheduled.append(True),
    )

    with TestClient(app):
        pass

    assert scheduled == [True]


def test_deep_research_exposes_typed_source_policy_schema():
    tools = asyncio.run(mcp.list_tools())
    deep_research = next(tool for tool in tools if tool.name == "deep_research")
    schema_text = str(deep_research.inputSchema)

    assert "SourcePolicyInput" in schema_text
    assert "required_sources" in schema_text
    assert "min_accepted_sources" in schema_text
    assert "independent_judge_required" in schema_text


def test_returned_source_policy_round_trips_through_public_schema():
    payload = {
        "version": "source_policy.v1",
        "enforcement": "strict",
        "discovery_mode": "required_only",
        "allowed_domains": ["example.com"],
        "denied_domains": [],
        "required_sources": [
            {
                "id": "authority",
                "family": "standard",
                "url": "https://example.com/evidence",
            }
        ],
        "min_accepted_sources": 1,
        "min_content_chars": 100,
        "require_title": True,
        "require_required_sources_cited": True,
        "independent_judge_required": True,
    }

    parsed = SourcePolicyInput.model_validate(payload)
    assert parsed.model_dump(exclude_none=True) == payload
    with pytest.raises(ValidationError):
        SourcePolicyInput.model_validate({**payload, "version": "source_policy.v999"})
