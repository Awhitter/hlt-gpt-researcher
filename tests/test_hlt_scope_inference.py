import json
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from backend.server import hlt_scope_inference as inference


ENV_KEYS = [
    "HLT_SCOPE_INFERENCE",
    "HLT_SCOPE_INFERENCE_LLM",
    "HLT_SCOPE_INFERENCE_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "FAST_LLM",
]


def clear_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_repo_mentions_select_codebase(monkeypatch):
    clear_env(monkeypatch)

    result = inference.infer_research_scope(
        "How does ScraperVault hand applications to nursing-mastery?"
    )

    assert result["scopes"] == ["codebase"]
    assert result["llm_used"] is False
    assert any("ScraperVault" in reason for reason in result["reasons"]["codebase"])


@pytest.mark.parametrize(
    "question",
    [
        "What attributes do we capture for a nurse?",
        "When do we capture email?",
        "How does job search work?",
        "What onboarding questions do we ask, and how can they be changed?",
        "Do we store emails in Marketo?",
    ],
)
def test_natural_product_questions_select_internal_code_research(monkeypatch, question):
    clear_env(monkeypatch)
    result = inference.infer_research_scope(question)
    assert "codebase" in result["scopes"]
    assert result["llm_used"] is False


def test_registry_and_metrics_signals_select_their_scopes(monkeypatch):
    clear_env(monkeypatch)

    result = inference.infer_research_scope(
        "Which Katailyst2 registry playbooks cover our conversion rate dashboards?"
    )

    assert "cms" in result["scopes"]
    assert "metrics" in result["scopes"]


def test_generic_query_selects_nothing(monkeypatch):
    clear_env(monkeypatch)

    result = inference.infer_research_scope("NCLEX pass rates 2026 by state")

    assert result["scopes"] == []
    assert result["candidates"] == []
    assert result["llm_used"] is False


def test_weak_signals_are_dropped_without_llm(monkeypatch):
    clear_env(monkeypatch)  # no OPENAI_API_KEY -> no tiebreak

    result = inference.infer_research_scope("Are leads improving this month?")

    assert result["scopes"] == []
    assert result["candidates"] == ["metrics"]
    assert result["llm_used"] is False


def test_llm_tiebreak_confirms_weak_candidates(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setattr(inference, "_llm_tiebreak", lambda task, candidates: {"metrics"})

    result = inference.infer_research_scope("Are leads improving this month?")

    assert result["scopes"] == ["metrics"]
    assert result["llm_used"] is True
    assert "confirmed by LLM tiebreak" in result["reasons"]["metrics"]


def test_llm_tiebreak_can_reject_all_candidates(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setattr(inference, "_llm_tiebreak", lambda task, candidates: set())

    result = inference.infer_research_scope("Are leads improving this month?")

    assert result["scopes"] == []
    assert result["llm_used"] is True


def test_readiness_filter_skips_unready_scopes(monkeypatch):
    clear_env(monkeypatch)

    result = inference.infer_research_scope(
        "Where does the ScraperVault repo store applications?",
        readiness={"codebase": "unavailable"},
    )

    assert result["scopes"] == []
    assert result["skipped_unready"] == ["codebase"]


def test_partial_readiness_still_activates(monkeypatch):
    clear_env(monkeypatch)

    result = inference.infer_research_scope(
        "Where does the ScraperVault repo store applications?",
        readiness={"codebase": "partial"},
    )

    assert result["scopes"] == ["codebase"]
    assert result["skipped_unready"] == []


def test_inference_kill_switch(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("HLT_SCOPE_INFERENCE", "0")

    result = inference.infer_research_scope(
        "How does ScraperVault hand applications to nursing-mastery?"
    )

    assert result["scopes"] == []
    assert result["candidates"] == []


def test_qbank_and_firecrawl_are_never_inferable():
    assert "qbank" not in inference.INFERABLE_SCOPES
    assert "firecrawl" not in inference.INFERABLE_SCOPES


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_llm_tiebreak_parses_openai_response(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        inference.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse(
            {"choices": [{"message": {"content": '{"scopes": ["metrics", "bogus"]}'}}]}
        ),
    )

    approved = inference._llm_tiebreak("Are leads improving?", ["metrics"])

    assert approved == {"metrics"}


def test_llm_tiebreak_returns_none_on_failure(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    def _boom(request, timeout):
        raise TimeoutError("slow")

    monkeypatch.setattr(inference.urllib.request, "urlopen", _boom)

    assert inference._llm_tiebreak("Are leads improving?", ["metrics"]) is None


def test_llm_tiebreak_disabled_without_key(monkeypatch):
    clear_env(monkeypatch)

    assert inference._llm_tiebreak("Are leads improving?", ["metrics"]) is None
