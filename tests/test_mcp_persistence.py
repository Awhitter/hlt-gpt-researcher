import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import gpt_researcher.research_run_store as run_store_module
import mcp_server.tools as mcp_tools


class FakeGPTResearcher:
    def __init__(self, query, report_type="research_report", report_source="web", tone=None, **kwargs):
        self.query = query
        self.report_type = report_type
        self.report_source = report_source
        self.tone = tone
        self.context = []
        self.research_sources = []
        self.visited_urls = set()
        self.costs = 0.0
        self.cfg = SimpleNamespace(
            max_search_results_per_query=5,
            image_generation_enabled=False,
        )
        self.research_images = [
            {
                "url": "https://example.com/alec.jpg",
                "source_url": "https://example.com/profile",
                "alt_text": "Alec Whitters",
            }
        ]
        self.write_calls = 0

    async def conduct_research(self):
        self.context = [{"finding": f"context for {self.query}"}]
        self.research_sources = [
            {
                "title": "Example",
                "url": "https://example.com/research",
                "content": "source body",
            }
        ]
        self.visited_urls = {"https://example.com/research"}
        self.costs = 0.25

    async def write_report(self, custom_prompt=""):
        self.write_calls += 1
        return f"# Report for {self.query}\n\n{custom_prompt}\n\n{self.context}"

    def get_research_context(self):
        return self.context

    def get_research_sources(self):
        return self.research_sources

    def get_source_urls(self):
        return list(self.visited_urls)

    def get_costs(self):
        return getattr(self, "research_costs", self.costs)

    def get_research_images(self):
        return self.research_images

    def get_all_research_images(self):
        return self.research_images


class StrictFakeGPTResearcher(FakeGPTResearcher):
    def __init__(self, *args, source_urls=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.source_urls = source_urls or []
        self.source_rejections = []
        self.research_images = []
        self.cfg.smart_llm_model = "judge-model"
        self.cfg.smart_llm_provider = "judge-provider"
        self.cfg.smart_token_limit = 4_000
        self.cfg.llm_kwargs = {}

    async def conduct_research(self):
        self.research_sources = [
            {
                "title": "Required authority",
                "url": url,
                "raw_content": "Authoritative evidence " * 20,
            }
            for url in self.source_urls
        ]
        self.context = "\n\n".join(
            f"Source: {source['url']}\nTitle: {source['title']}\nContent: {source['raw_content']}"
            for source in self.research_sources
        )
        self.visited_urls = set(self.source_urls)
        self.costs = 0.25

    async def write_report(self, custom_prompt=""):
        self.write_calls += 1
        citations = "\n".join(
            f"Supported claim ([source]({url}))." for url in self.source_urls
        )
        return f"# Report\n\nPASS\n\n{custom_prompt}\n\n{citations}"


def _reset_store(monkeypatch, tmp_path):
    monkeypatch.setenv("RESEARCH_RUN_STORE_PATH", str(tmp_path / "runs.sqlite3"))
    monkeypatch.setenv("OUTPUTS_DIR", str(tmp_path / "outputs"))
    run_store_module._store = None
    mcp_tools.clear_hot_cache()


def _allow_strict_runtime(monkeypatch):
    async def available(_policy):
        return None

    monkeypatch.setattr(mcp_tools, "_require_strict_scraper_runtime", available)


def test_mcp_deep_research_survives_hot_cache_loss(monkeypatch, tmp_path):
    _reset_store(monkeypatch, tmp_path)
    monkeypatch.setattr(mcp_tools, "GPTResearcher", FakeGPTResearcher)

    result = asyncio.run(mcp_tools.deep_research_tool("restart-safe research"))
    assert result["status"] == "success"
    research_id = result["research_id"]

    mcp_tools.clear_hot_cache()

    context = asyncio.run(mcp_tools.get_research_context_tool(research_id))
    sources = asyncio.run(mcp_tools.get_research_sources_tool(research_id))

    assert context["status"] == "success"
    assert context["context"] == [{"finding": "context for restart-safe research"}]
    assert sources["status"] == "success"
    assert sources["sources"][0]["url"] == "https://example.com/research"

    images = asyncio.run(mcp_tools.get_research_images_tool(research_id))
    assert images["image_count"] == 1
    assert images["images"][0]["source_url"] == "https://example.com/profile"


def test_mcp_depth_sets_source_breadth(monkeypatch, tmp_path):
    _reset_store(monkeypatch, tmp_path)
    monkeypatch.setattr(mcp_tools, "GPTResearcher", FakeGPTResearcher)

    result = asyncio.run(mcp_tools.deep_research_tool("broad research", depth="deep"))

    assert result["status"] == "success"
    item = asyncio.run(mcp_tools._get_research(result["research_id"]))
    assert item.researcher.cfg.max_search_results_per_query == 12
    assert result["max_sources_per_query"] == 12
    assert result["image_count"] == 1


def test_internal_deterministic_research_id_is_persisted_without_changing_mcp_default(
    monkeypatch, tmp_path
):
    _reset_store(monkeypatch, tmp_path)
    monkeypatch.setattr(mcp_tools, "GPTResearcher", FakeGPTResearcher)
    deterministic_id = "8b2c16e0-6217-53e1-9fe3-e05ca23e0652"

    result = asyncio.run(
        mcp_tools.deep_research_tool(
            "automation-owned identity",
            _research_id=deterministic_id,
        )
    )

    assert result["status"] == "success"
    assert result["research_id"] == deterministic_id
    assert (
        run_store_module.get_research_run_store().get_run(deterministic_id)[
            "query"
        ]
        == "automation-owned identity"
    )


def test_mcp_write_report_hydrates_from_sqlite_after_restart(monkeypatch, tmp_path):
    _reset_store(monkeypatch, tmp_path)
    monkeypatch.setattr(mcp_tools, "GPTResearcher", FakeGPTResearcher)

    result = asyncio.run(mcp_tools.deep_research_tool("write after restart"))
    research_id = result["research_id"]
    mcp_tools.clear_hot_cache()

    report = asyncio.run(mcp_tools.write_report_tool(research_id, "Use bullets."))

    assert report["status"] == "success"
    assert "write after restart" in report["report"]
    assert Path(report["md_path"]).exists()

    run = run_store_module.get_research_run_store().get_run(research_id)
    assert run["status"] == "completed"
    assert run["md_path"] == report["md_path"]
    assert run["sources"][0]["title"] == "Example"
    assert report["costs"] == 0.25


def test_strict_runtime_preflight_fails_before_researcher_construction(
    monkeypatch, tmp_path
):
    _reset_store(monkeypatch, tmp_path)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    constructed = []

    class UnexpectedResearcher:
        def __init__(self, *_args, **_kwargs):
            constructed.append(True)

    monkeypatch.setattr(mcp_tools, "GPTResearcher", UnexpectedResearcher)
    result = asyncio.run(
        mcp_tools.deep_research_tool(
            "strict preflight",
            source_policy={
                "enforcement": "strict",
                "required_sources": ["https://example.com/evidence"],
            },
        )
    )

    assert result["status"] == "error"
    assert result["error_code"] == "strict_scraper_unavailable"
    assert "FIRECRAWL_API_KEY" in result["message"]
    assert constructed == []
    assert run_store_module.get_research_run_store().list_runs() == []


def test_strict_runtime_preflight_rejects_missing_sdk_and_private_server(monkeypatch):
    policy = mcp_tools.SourcePolicy.from_value(
        {
            "enforcement": "strict",
            "required_sources": ["https://example.com/evidence"],
        }
    )
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setattr(mcp_tools, "find_spec", lambda _name: None)
    with pytest.raises(mcp_tools.StrictScraperUnavailable, match="Python package"):
        asyncio.run(mcp_tools._require_strict_scraper_runtime(policy))

    monkeypatch.setattr(mcp_tools, "find_spec", lambda _name: object())
    monkeypatch.setenv("FIRECRAWL_SERVER_URL", "http://127.0.0.1:3002")
    with pytest.raises(mcp_tools.StrictScraperUnavailable, match="public Firecrawl"):
        asyncio.run(mcp_tools._require_strict_scraper_runtime(policy))

    monkeypatch.setenv("FIRECRAWL_SERVER_URL", "https://api.firecrawl.dev")

    def reject_required_target(url, **_kwargs):
        if "example.com/evidence" in url:
            raise mcp_tools.SourcePolicyError(
                "source URL is not public: dns_resolved_non_public_ip"
            )

    monkeypatch.setattr(mcp_tools, "require_public_source_url", reject_required_target)
    with pytest.raises(mcp_tools.StrictScraperUnavailable, match="required-source"):
        asyncio.run(mcp_tools._require_strict_scraper_runtime(policy))


def test_strict_manifest_failure_is_persisted_and_readable(monkeypatch, tmp_path):
    _reset_store(monkeypatch, tmp_path)
    _allow_strict_runtime(monkeypatch)
    monkeypatch.setattr(mcp_tools, "GPTResearcher", FakeGPTResearcher)
    policy = {
        "enforcement": "strict",
        "discovery_mode": "required_only",
        "required_sources": [
            {
                "id": "required",
                "family": "authority",
                "url": "https://authority.example/required",
            }
        ],
    }

    result = asyncio.run(
        mcp_tools.deep_research_tool("strict failure", source_policy=policy)
    )

    assert result["status"] == "error"
    assert result["error_code"] == "source_manifest_failed"
    research_id = result["research_id"]
    mcp_tools.clear_hot_cache()
    monkeypatch.setattr(
        mcp_tools,
        "GPTResearcher",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("durable readback must not construct GPTResearcher")
        ),
    )
    readback = asyncio.run(mcp_tools.get_research_sources_tool(research_id))
    assert readback["status"] == "success"
    assert readback["source_manifest"]["status"] == "failed"
    run = run_store_module.get_research_run_store().get_run(research_id)
    assert run["status"] == "failed"
    assert run["error_code"] == "source_manifest_failed"
    retry = asyncio.run(mcp_tools.write_report_tool(research_id))
    assert retry["status"] == "error"
    assert "current status is failed" in retry["message"]


def test_strict_report_needs_independent_judge_even_when_draft_says_pass(
    monkeypatch, tmp_path
):
    _reset_store(monkeypatch, tmp_path)
    _allow_strict_runtime(monkeypatch)
    monkeypatch.setattr(mcp_tools, "GPTResearcher", StrictFakeGPTResearcher)

    async def reject_judgment(_item, _report):
        return {
            "verdict": "repair_required",
            "findings": [{"code": "unsupported_claim", "severity": "high"}],
        }

    monkeypatch.setattr(mcp_tools, "_run_independent_source_judge", reject_judgment)
    policy = {
        "enforcement": "strict",
        "discovery_mode": "required_only",
        "required_sources": [
            {
                "id": "required",
                "family": "authority",
                "url": "https://authority.example/required",
            }
        ],
    }
    research = asyncio.run(
        mcp_tools.deep_research_tool("strict report", source_policy=policy)
    )
    assert research["status"] == "success"

    result = asyncio.run(mcp_tools.write_report_tool(research["research_id"]))

    assert result["status"] == "error"
    assert result["error_code"] == "report_quality_failed"
    assert result["publishable"] is False
    assert result["report_quality"]["status"] == "failed"
    run = run_store_module.get_research_run_store().get_run(research["research_id"])
    assert run["status"] == "failed"
    assert run["report_quality"]["independent_judgment"]["verdict"] == "repair_required"
    item = asyncio.run(mcp_tools._get_research(research["research_id"]))
    assert item.researcher.write_calls == 1
    retry = asyncio.run(mcp_tools.write_report_tool(research["research_id"]))
    assert retry["status"] == "error"
    assert item.researcher.write_calls == 1


def test_report_lock_deduplicates_concurrent_identical_requests(monkeypatch, tmp_path):
    _reset_store(monkeypatch, tmp_path)
    monkeypatch.setattr(mcp_tools, "GPTResearcher", FakeGPTResearcher)
    research = asyncio.run(mcp_tools.deep_research_tool("concurrent report"))
    item = asyncio.run(mcp_tools._get_research(research["research_id"]))

    async def run_concurrently():
        return await asyncio.gather(
            mcp_tools.write_report_tool(research["research_id"], "Use bullets."),
            mcp_tools.write_report_tool(research["research_id"], "Use bullets."),
        )

    first, second = asyncio.run(run_concurrently())

    assert first["status"] == second["status"] == "success"
    assert item.researcher.write_calls == 1
    assert second["idempotent_readback"] is True


def test_unknown_report_ids_do_not_leak_per_run_locks(monkeypatch, tmp_path):
    _reset_store(monkeypatch, tmp_path)

    async def request_unknown_ids():
        return [
            await mcp_tools.write_report_tool(f"unknown-{index}")
            for index in range(200)
        ]

    results = asyncio.run(request_unknown_ids())

    assert all(result["status"] == "error" for result in results)
    assert mcp_tools._research_by_id == {}
    assert mcp_tools._report_locks == {}


def test_completed_report_regenerates_for_a_different_custom_prompt(
    monkeypatch, tmp_path
):
    _reset_store(monkeypatch, tmp_path)
    monkeypatch.setattr(mcp_tools, "GPTResearcher", FakeGPTResearcher)
    research = asyncio.run(mcp_tools.deep_research_tool("prompt-specific report"))
    item = asyncio.run(mcp_tools._get_research(research["research_id"]))

    first = asyncio.run(
        mcp_tools.write_report_tool(research["research_id"], "Use bullets.")
    )
    second = asyncio.run(
        mcp_tools.write_report_tool(research["research_id"], "Use a table.")
    )

    assert first["status"] == second["status"] == "success"
    assert "Use a table." in second["report"]
    assert second.get("idempotent_readback") is not True
    assert item.researcher.write_calls == 2


def test_strict_scope_bypasses_internal_scope_injection(monkeypatch, tmp_path):
    _reset_store(monkeypatch, tmp_path)
    _allow_strict_runtime(monkeypatch)
    monkeypatch.setattr(mcp_tools, "GPTResearcher", StrictFakeGPTResearcher)
    monkeypatch.setattr(
        mcp_tools,
        "_prepare_scoped_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("strict requests must not use HLT scope injection")
        ),
    )
    query = "raw strict query"
    item, scope = asyncio.run(
        mcp_tools._conduct_research(
            query,
            source_policy={
                "enforcement": "strict",
                "required_sources": ["https://example.com/evidence"],
            },
        )
    )

    assert item.researcher.query == query
    assert scope is None
    assert item.researcher.cfg.scraper == "firecrawl"


def test_rejected_revision_preserves_last_accepted_report(monkeypatch, tmp_path):
    _reset_store(monkeypatch, tmp_path)
    _allow_strict_runtime(monkeypatch)
    monkeypatch.setattr(mcp_tools, "GPTResearcher", StrictFakeGPTResearcher)

    async def prompt_sensitive_judgment(_item, report):
        if "Reject this candidate" in report:
            return {
                "verdict": "repair_required",
                "findings": [{"code": "unsupported_claim", "severity": "high"}],
            }
        return {
            "verdict": "pass",
            "findings": [],
            "claim_checks": [
                {
                    "claim": "Supported claim",
                    "supported": True,
                    "source_urls": ["https://example.com/evidence"],
                }
            ],
        }

    monkeypatch.setattr(
        mcp_tools, "_run_independent_source_judge", prompt_sensitive_judgment
    )
    research = asyncio.run(
        mcp_tools.deep_research_tool(
            "revision-safe report",
            source_policy={
                "enforcement": "strict",
                "required_sources": ["https://example.com/evidence"],
            },
        )
    )
    accepted = asyncio.run(
        mcp_tools.write_report_tool(research["research_id"], "Accepted format")
    )
    accepted_text = Path(accepted["report_path"]).read_text(encoding="utf-8")

    rejected = asyncio.run(
        mcp_tools.write_report_tool(
            research["research_id"], "Reject this candidate"
        )
    )
    run = run_store_module.get_research_run_store().get_run(research["research_id"])

    assert rejected["status"] == "error"
    assert rejected["accepted_revision_preserved"] is True
    assert run["status"] == "completed"
    assert run["report_path"] == accepted["report_path"]
    assert run["report_quality"]["publishable"] is True
    assert Path(run["report_path"]).read_text(encoding="utf-8") == accepted_text
    assert run["rejected_report_path"] == rejected["report_path"]
    assert run["rejected_report_quality"]["publishable"] is False
    assert Path(run["rejected_report_path"]).read_text(encoding="utf-8") == rejected["draft_report"]
    readback = asyncio.run(
        mcp_tools.get_research_context_tool(research["research_id"])
    )
    assert readback["report_path"] == accepted["report_path"]
    assert readback["rejected_draft_report"] == rejected["draft_report"]
    item = asyncio.run(mcp_tools._get_research(research["research_id"]))
    replay = asyncio.run(
        mcp_tools.write_report_tool(
            research["research_id"], "Reject this candidate"
        )
    )
    assert replay["idempotent_readback"] is True
    assert item.researcher.write_calls == 2
