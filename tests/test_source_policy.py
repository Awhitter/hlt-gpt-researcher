import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import gpt_researcher as gpt_researcher_package
import gpt_researcher.source_policy as source_policy_module
import gpt_researcher.scraper.scraper as scraper_module
from gpt_researcher.scraper.firecrawl.firecrawl import FireCrawl
from gpt_researcher.scraper.scraper import Scraper
from gpt_researcher.skills.researcher import ResearchConductor
from gpt_researcher.skills.deep_research import DeepResearchSkill
from gpt_researcher.source_policy import (
    MAX_REQUIRED_SOURCES,
    MAX_SOURCE_DOMAIN_CHARS,
    MAX_SOURCE_FAMILY_CHARS,
    MAX_SOURCE_ID_CHARS,
    MAX_SOURCE_URL_CHARS,
    MAX_STRICT_REPORT_CHARS,
    SourcePolicy,
    SourcePolicyError,
    build_report_quality,
    build_source_manifest,
    canonicalize_url,
    merge_source_records,
    public_source_url_allowed,
    source_content,
    source_url_allowed,
)
from mcp_server.tools import (
    _parse_independent_judgment,
    _select_relevant_source_excerpts,
)


REQUIRED = [
    {
        "id": "ancc-ptap",
        "family": "ANCC",
        "url": "https://www.nursingworld.org/organizational-programs/accreditation/ptap/",
    },
    {
        "id": "ccne-standards",
        "family": "CCNE",
        "url": "https://www.aacnnursing.org/ccne-accreditation/accreditation-resources/standards-procedures-guidelines",
    },
    {
        "id": "ncsbn-transition",
        "family": "NCSBN",
        "url": "https://www.ncsbn.org/nursing-regulation/practice/transition-to-practice.page",
    },
    {
        "id": "peer-reviewed-outcomes",
        "family": "peer-reviewed",
        "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC10907523/",
    },
]


def strict_policy():
    return SourcePolicy.from_value(
        {
            "enforcement": "strict",
            "discovery_mode": "required_only",
            "required_sources": REQUIRED,
            "min_content_chars": 100,
        }
    )


def source(required, *, content="Evidence " * 20):
    return {
        "url": required["url"],
        "title": required["family"],
        "raw_content": content,
    }


def test_strict_required_only_policy_rejects_every_other_domain_and_url():
    policy = strict_policy()
    assert source_url_allowed(policy, REQUIRED[0]["url"]) == (True, None)
    assert source_url_allowed(
        policy,
        "https://www.registerednursing.org/articles/nurse-residency-program-accreditation/",
    ) == (False, "outside_allowed_domains")


def test_manifest_reproduces_failed_canary_boundaries_without_model_judgment():
    policy = strict_policy()
    sources = [
        source(REQUIRED[0]),
        source(REQUIRED[1]),
        source(REQUIRED[3]),
        source(REQUIRED[3]),
        {
            "url": "https://www.registerednursing.org/articles/nurse-residency-program-accreditation/",
            "title": "Commercial explainer",
            "raw_content": "Commercial evidence " * 20,
        },
        {
            "url": "https://example.com/unknown",
            "title": "Unknown",
            "raw_content": "",
        },
    ]

    manifest = build_source_manifest(
        policy,
        sources,
        images=[
            {
                "url": "https://mc.yandex.com/watch/28584306",
                "source_url": "https://translate.yandex.com/ocr",
                "alt_text": "",
            }
        ],
    )

    assert manifest["status"] == "failed"
    assert manifest["duplicate_count"] == 1
    assert any(
        blocker.get("code") == "required_source_missing"
        and blocker.get("family") == "NCSBN"
        for blocker in manifest["blockers"]
    )
    assert any(
        "outside_allowed_domains" in entry["reasons"]
        for entry in manifest["rejected_sources"]
    )
    assert manifest["images"][0]["status"] == "rejected"


def test_report_cannot_self_declare_pass_or_cite_unadmitted_source():
    policy = strict_policy()
    manifest = build_source_manifest(policy, [source(item) for item in REQUIRED])
    report = """# Result

PASS

Commercial summary ([source](https://www.registerednursing.org/articles/nurse-residency-program-accreditation/)).
"""

    quality = build_report_quality(
        policy,
        manifest,
        report,
        {"verdict": "pass", "findings": []},
    )

    assert quality["status"] == "failed"
    assert quality["publishable"] is False
    assert quality["unadmitted_citations"] == [
        canonicalize_url(
            "https://www.registerednursing.org/articles/nurse-residency-program-accreditation/"
        )
    ]
    assert len(quality["missing_required_citations"]) == 4


def test_report_pass_requires_manifest_citations_and_independent_judge():
    policy = strict_policy()
    manifest = build_source_manifest(policy, [source(item) for item in REQUIRED])
    report = "\n".join(
        f"Supported {item['family']} finding ([evidence]({item['url']}))."
        for item in REQUIRED
    )

    quality = build_report_quality(
        policy,
        manifest,
        report,
        {"verdict": "pass", "findings": [], "claim_checks": []},
    )

    assert quality["status"] == "passed"
    assert quality["publishable"] is True
    assert quality["missing_required_citations"] == []


def test_report_fails_when_judge_says_pass_but_returns_blocking_finding():
    policy = strict_policy()
    manifest = build_source_manifest(policy, [source(item) for item in REQUIRED])
    report = "\n".join(
        f"Supported {item['family']} finding ([evidence]({item['url']}))."
        for item in REQUIRED
    )

    quality = build_report_quality(
        policy,
        manifest,
        report,
        {
            "verdict": "pass",
            "findings": [
                {
                    "code": "unsupported_claim",
                    "severity": "high",
                    "claim": "The draft overstates the admitted evidence.",
                }
            ],
        },
    )

    assert quality["status"] == "failed"
    assert quality["publishable"] is False
    assert any(
        finding["code"] == "independent_judge_blocking_findings"
        for finding in quality["findings"]
    )


def test_strict_report_length_cap_prevents_unjudged_tail():
    policy = strict_policy()
    manifest = build_source_manifest(policy, [source(item) for item in REQUIRED])
    citations = "\n".join(item["url"] for item in REQUIRED)
    report = f"{citations}\n{'supported text ' * 4_000}unsupported tail"
    assert len(report) > MAX_STRICT_REPORT_CHARS

    quality = build_report_quality(
        policy,
        manifest,
        report,
        {
            "verdict": "pass",
            "findings": [],
            "claim_checks": [
                {
                    "claim": "Prefix claim",
                    "supported": True,
                    "source_urls": [REQUIRED[0]["url"]],
                }
            ],
        },
    )

    assert quality["status"] == "failed"
    assert any(
        finding["code"] == "strict_report_too_long"
        for finding in quality["findings"]
    )


def test_advisory_report_quality_has_no_strict_judge_contradiction():
    quality = build_report_quality(
        None,
        {"status": "passed", "accepted_sources": []},
        "Ordinary report",
        {"verdict": "not_required", "findings": []},
    )

    assert quality["status"] == "not_applicable"
    assert quality["publishable"] is True
    assert quality["findings"] == []


def test_canonical_url_dedupes_tracking_parameters_and_fragments():
    assert canonicalize_url(
        "https://Example.com/path/?utm_source=test&b=2&a=1#section"
    ) == "https://example.com/path?a=1&b=2"


def test_canonical_url_preserves_semantic_ref_and_source_parameters():
    assert canonicalize_url(
        "https://example.com/path?source=official&ref=standard&utm_source=test"
    ) == "https://example.com/path?ref=standard&source=official"


def test_invalid_port_is_rejected_without_raising():
    assert canonicalize_url("https://example.com:not-a-port/path") == ""
    assert source_url_allowed(strict_policy(), "https://example.com:not-a-port/path") == (
        False,
        "invalid_url",
    )


def test_strict_policy_fails_when_no_evidence_is_accepted():
    policy = SourcePolicy.from_value(
        {
            "enforcement": "strict",
            "discovery_mode": "allowed_domains",
            "allowed_domains": ["ncsbn.org"],
        }
    )

    manifest = build_source_manifest(policy, [])

    assert manifest["status"] == "failed"
    assert manifest["blockers"] == [
        {"code": "insufficient_accepted_sources", "accepted": 0, "required": 1}
    ]


def test_strict_policy_rejects_duplicate_ids_urls_and_oversized_manifests():
    duplicate_id = [
        {"id": "same", "family": "one", "url": "https://example.com/a"},
        {"id": "same", "family": "two", "url": "https://example.com/b"},
    ]
    with pytest.raises(SourcePolicyError, match="ids must be unique"):
        SourcePolicy.from_value(
            {"enforcement": "strict", "required_sources": duplicate_id}
        )

    duplicate_url = [
        {"id": "one", "family": "one", "url": "https://example.com/a/"},
        {"id": "two", "family": "two", "url": "https://example.com/a"},
    ]
    with pytest.raises(SourcePolicyError, match="URLs must be unique"):
        SourcePolicy.from_value(
            {"enforcement": "strict", "required_sources": duplicate_url}
        )

    oversized = [
        {"id": str(index), "family": "test", "url": f"https://example.com/{index}"}
        for index in range(MAX_REQUIRED_SOURCES + 1)
    ]
    with pytest.raises(SourcePolicyError, match="at most"):
        SourcePolicy.from_value(
            {"enforcement": "strict", "required_sources": oversized}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "i" * (MAX_SOURCE_ID_CHARS + 1)),
        ("family", "f" * (MAX_SOURCE_FAMILY_CHARS + 1)),
        ("url", "https://example.com/" + "u" * MAX_SOURCE_URL_CHARS),
    ],
)
def test_required_source_fields_are_bounded(field, value):
    required = {
        "id": "required",
        "family": "authority",
        "url": "https://example.com/evidence",
        field: value,
    }
    with pytest.raises(SourcePolicyError, match="at most"):
        SourcePolicy.from_value({"required_sources": [required]})


def test_source_domains_are_bounded():
    with pytest.raises(SourcePolicyError, match="at most"):
        SourcePolicy.from_value(
            {"allowed_domains": ["d" * (MAX_SOURCE_DOMAIN_CHARS + 1)]}
        )
    with pytest.raises(SourcePolicyError, match="oversized domain"):
        SourcePolicy.from_value(
            {
                "required_sources": [
                    f"https://{'d' * (MAX_SOURCE_DOMAIN_CHARS + 1)}/evidence"
                ]
            }
        )


def test_source_policy_version_round_trips_and_rejects_unknown_versions():
    policy = strict_policy()
    assert SourcePolicy.from_value(policy.to_dict()) == policy
    with pytest.raises(SourcePolicyError, match="version must be"):
        SourcePolicy.from_value({"version": "source_policy.v999"})


def test_strict_acceptance_invariants_cannot_be_disabled():
    for override in (
        {"require_title": False},
        {"require_required_sources_cited": False},
        {"independent_judge_required": False},
        {"min_content_chars": 99},
    ):
        with pytest.raises(SourcePolicyError, match="strict source policies require"):
            SourcePolicy.from_value(
                {
                    "enforcement": "strict",
                    "required_sources": [REQUIRED[0]],
                    **override,
                }
            )


def test_strict_policy_rejects_private_and_private_dns_targets():
    policy = SourcePolicy.from_value(
        {
            "enforcement": "strict",
            "allowed_domains": ["localhost", "example.com"],
            "discovery_mode": "allowed_domains",
        }
    )
    assert source_url_allowed(policy, "http://127.0.0.1/private") == (
        False,
        "non_public_ip",
    )

    def private_resolver(*_args, **_kwargs):
        return [(2, 1, 6, "", ("10.0.0.8", 443))]

    assert public_source_url_allowed(
        "https://example.com/evidence",
        resolve_dns=True,
        resolver=private_resolver,
    ) == (False, "dns_resolved_non_public_ip")


def test_duplicate_unknown_title_is_replaced_by_real_scrape_metadata():
    merged = merge_source_records(
        {"url": "https://example.com/a", "title": "Unknown", "body": "snippet"},
        {
            "url": "https://example.com/a",
            "title": "Authoritative program standard",
            "raw_content": "Evidence " * 20,
        },
    )

    assert merged["title"] == "Authoritative program standard"
    assert merged["raw_content"].startswith("Evidence")


def test_bare_or_malformed_judge_pass_fails_closed():
    judgment = _parse_independent_judgment(
        '{"verdict":"pass","findings":[],"claim_checks":[]}'
    )
    assert judgment["verdict"] == "error"
    assert judgment["findings"][0]["code"] == "judge_claim_checks_missing"
    assert _parse_independent_judgment("PASS")["verdict"] == "error"


def test_judge_excerpt_finds_support_well_beyond_leading_boilerplate():
    canonical = "https://example.com/standard"
    content = (
        "navigation and boilerplate " * 250
        + "Neonatal cardiac screening requires a ninety five percent threshold."
        + " footer" * 250
    )
    report = (
        "Neonatal cardiac screening uses a ninety five percent threshold "
        f"([standard]({canonical}))."
    )

    excerpts = _select_relevant_source_excerpts(
        content,
        report=report,
        canonical_url=canonical,
        budget_chars=2_000,
    )

    assert any("ninety five percent threshold" in item["text"] for item in excerpts)
    assert any(item["char_start"] > 2_000 for item in excerpts)


def test_strict_scraper_selects_firecrawl_and_rejects_local_backends(monkeypatch):
    monkeypatch.setattr(scraper_module, "check_pkg", lambda *_args: None)
    policy = SourcePolicy.from_value(
        {
            "enforcement": "strict",
            "required_sources": [
                {
                    "id": "required",
                    "family": "authority",
                    "url": "https://example.com/evidence",
                }
            ],
        }
    )
    scraper = Scraper(
        policy.required_urls,
        "test-agent",
        "firecrawl",
        worker_pool=SimpleNamespace(),
        enforce_public_network=True,
        source_policy=policy,
    )
    assert scraper.get_scraper(policy.required_urls[0]) is FireCrawl
    scraper.scraper = "bs"
    with pytest.raises(SourcePolicyError, match="Firecrawl remote scraper"):
        scraper.get_scraper(policy.required_urls[0])


def test_strict_scraper_rejects_disallowed_redirect(monkeypatch):
    monkeypatch.setattr(scraper_module, "check_pkg", lambda *_args: None)
    monkeypatch.setattr(
        source_policy_module, "require_public_source_url", lambda *_args, **_kwargs: None
    )
    policy = SourcePolicy.from_value(
        {
            "enforcement": "strict",
            "required_sources": ["https://example.com/evidence"],
        }
    )
    scraper = Scraper(
        policy.required_urls,
        "test-agent",
        "firecrawl",
        worker_pool=SimpleNamespace(),
        enforce_public_network=True,
        source_policy=policy,
    )
    response = SimpleNamespace(
        url="https://example.com/evidence",
        headers={"location": "https://evil.example/redirect"},
    )

    with pytest.raises(SourcePolicyError, match="outside_"):
        scraper._validate_response_url(response)


def test_private_dns_fetch_failure_is_recorded_as_blocked_candidate(monkeypatch):
    monkeypatch.setattr(scraper_module, "check_pkg", lambda *_args: None)

    def reject_private_dns(*_args, **_kwargs):
        raise SourcePolicyError("source URL is not public: dns_resolved_non_public_ip")

    monkeypatch.setattr(scraper_module, "require_policy_source_url", reject_private_dns)
    failures = []

    class DummyPool:
        executor = None

        @asynccontextmanager
        async def throttle(self):
            yield

    policy = SourcePolicy.from_value(
        {
            "enforcement": "strict",
            "required_sources": ["https://example.com/evidence"],
        }
    )
    scraper = Scraper(
        policy.required_urls,
        "test-agent",
        "firecrawl",
        worker_pool=DummyPool(),
        enforce_public_network=True,
        source_policy=policy,
        failure_callback=failures.append,
    )

    result = asyncio.run(
        scraper.extract_data_from_url(policy.required_urls[0], scraper.session)
    )

    assert result["raw_content"] is None
    assert failures == [
        {
            "url": policy.required_urls[0],
            "reason": "source URL is not public: dns_resolved_non_public_ip",
        }
    ]


def test_strict_firecrawl_requires_final_url_provenance():
    policy = SourcePolicy.from_value(
        {
            "enforcement": "strict",
            "required_sources": ["https://example.com/evidence"],
        }
    )
    firecrawl = FireCrawl.__new__(FireCrawl)
    firecrawl.link = policy.required_urls[0]
    firecrawl.session = SimpleNamespace(
        _gptr_enforce_public_network=True,
        _gptr_source_policy=policy,
    )
    firecrawl.firecrawl = SimpleNamespace(
        scrape=lambda **_kwargs: SimpleNamespace(
            markdown="Authoritative evidence " * 20,
            metadata=SimpleNamespace(
                error=None,
                status_code=200,
                source_url=None,
                url=None,
                title="Authority",
            ),
        )
    )

    with pytest.raises(SourcePolicyError, match="resolved URL provenance"):
        firecrawl.scrape()


def test_strict_firecrawl_rejects_disallowed_resolved_url(monkeypatch):
    monkeypatch.setattr(
        source_policy_module, "require_public_source_url", lambda *_args, **_kwargs: None
    )
    policy = SourcePolicy.from_value(
        {
            "enforcement": "strict",
            "required_sources": ["https://example.com/evidence"],
        }
    )
    captured = {}

    def scrape(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            markdown="Authoritative evidence " * 20,
            metadata=SimpleNamespace(
                error=None,
                status_code=200,
                source_url="https://example.com/evidence",
                url="https://evil.example/redirect",
                title="Authority",
            ),
        )

    firecrawl = FireCrawl.__new__(FireCrawl)
    firecrawl.link = policy.required_urls[0]
    firecrawl.session = SimpleNamespace(
        _gptr_enforce_public_network=True,
        _gptr_source_policy=policy,
    )
    firecrawl.firecrawl = SimpleNamespace(scrape=scrape)

    with pytest.raises(SourcePolicyError, match="outside_"):
        firecrawl.scrape()
    assert captured["max_age"] == 0
    assert captured["store_in_cache"] is False


def test_strict_firecrawl_rejects_cross_required_source_attestation(monkeypatch):
    monkeypatch.setattr(
        source_policy_module, "require_public_source_url", lambda *_args, **_kwargs: None
    )
    policy = SourcePolicy.from_value(
        {
            "enforcement": "strict",
            "required_sources": [
                "https://example.com/source-a",
                "https://example.com/source-b",
            ],
        }
    )
    firecrawl = FireCrawl.__new__(FireCrawl)
    firecrawl.link = policy.required_urls[0]
    firecrawl.session = SimpleNamespace(
        _gptr_enforce_public_network=True,
        _gptr_source_policy=policy,
    )
    firecrawl.firecrawl = SimpleNamespace(
        scrape=lambda **_kwargs: SimpleNamespace(
            markdown="Source B evidence " * 20,
            metadata=SimpleNamespace(
                error=None,
                status_code=200,
                source_url=policy.required_urls[1],
                url=policy.required_urls[1],
                title="Source B",
            ),
        )
    )

    with pytest.raises(SourcePolicyError, match="attestation did not match"):
        firecrawl.scrape()


def test_deep_research_aggregates_nested_scrape_rejections(monkeypatch):
    policy = SourcePolicy.from_value(
        {
            "enforcement": "strict",
            "required_sources": ["https://example.com/evidence"],
        }
    )

    class ParentResearcher:
        def __init__(self):
            self.cfg = SimpleNamespace(
                deep_research_breadth=1,
                deep_research_depth=1,
                deep_research_concurrency=1,
                config_path=None,
            )
            self.websocket = None
            self.tone = "Objective"
            self.headers = {}
            self.visited_urls = set()
            self.source_policy = policy
            self.mcp_configs = None
            self.mcp_strategy = None
            self.query = "parent query"
            self.log_handler = None
            self.source_rejections = []
            self.context = []
            self.research_sources = []

        def get_costs(self):
            return 0.0

        def add_source_rejection(self, url, reason, *, stage):
            record = {"url": url, "reason": reason, "stage": stage}
            if record not in self.source_rejections:
                self.source_rejections.append(record)

    class NestedResearcher:
        def __init__(self, *_args, **_kwargs):
            self.visited_urls = set()
            self.research_sources = []
            self.source_rejections = [
                {
                    "url": "https://example.com/evidence",
                    "reason": "source URL is not public: dns_resolution_failed",
                    "stage": "scrape",
                }
            ]

        async def conduct_research(self):
            return ""

    parent = ParentResearcher()
    skill = DeepResearchSkill(parent)

    async def plan(_query):
        return ["What does the authority say?"]

    async def queries(_query, num_queries=3):
        return [{"query": "nested query", "researchGoal": "verify authority"}]

    async def results(**_kwargs):
        return {"learnings": [], "followUpQuestions": [], "citations": {}}

    monkeypatch.setattr(gpt_researcher_package, "GPTResearcher", NestedResearcher)
    monkeypatch.setattr(skill, "generate_research_plan", plan)
    monkeypatch.setattr(skill, "generate_search_queries", queries)
    monkeypatch.setattr(skill, "process_research_results", results)

    asyncio.run(skill.run())

    assert parent.source_rejections == [
        {
            "url": "https://example.com/evidence",
            "reason": "source URL is not public: dns_resolution_failed",
            "stage": "scrape",
        }
    ]


def test_source_content_prefers_richest_cross_key_duplicate():
    merged = merge_source_records(
        {"url": "https://example.com/a", "raw_content": "short"},
        {"url": "https://example.com/a", "content": "rich evidence " * 30},
    )

    assert source_content(merged) == "rich evidence " * 30


def test_generated_image_uses_trusted_local_contract(tmp_path):
    image_path = tmp_path / "outputs" / "images" / "run-1" / "diagram.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"generated image")

    manifest = build_source_manifest(
        strict_policy(),
        [source(item) for item in REQUIRED],
        images=[
            {
                "kind": "generated",
                "url": "/outputs/images/run-1/diagram.png",
                "path": str(image_path),
                "alt_text": "Evidence map",
            }
        ],
    )

    assert manifest["status"] == "passed"
    assert manifest["images"][0]["status"] == "accepted"


def test_source_image_rejects_non_public_asset_even_from_admitted_page():
    manifest = build_source_manifest(
        strict_policy(),
        [source(item) for item in REQUIRED],
        images=[
            {
                "url": "http://127.0.0.1/private.jpg",
                "source_url": REQUIRED[0]["url"],
                "alt_text": "Private image",
            }
        ],
    )

    assert manifest["status"] == "failed"
    assert "non_public_image_url" in manifest["images"][0]["reasons"]


def test_retrieval_admission_blocks_disallowed_url_before_fetch():
    rejections = []
    researcher = SimpleNamespace(
        source_policy=strict_policy(),
        visited_urls=set(),
        verbose=False,
        add_source_rejection=lambda url, reason, stage: rejections.append(
            {"url": url, "reason": reason, "stage": stage}
        ),
    )
    conductor = ResearchConductor(researcher)

    admitted = asyncio.run(
        conductor._get_new_urls(
            [
                REQUIRED[0]["url"],
                "https://www.registerednursing.org/articles/nurse-residency-program-accreditation/",
            ]
        )
    )

    assert admitted == [REQUIRED[0]["url"]]
    assert rejections == [
        {
            "url": "https://www.registerednursing.org/articles/nurse-residency-program-accreditation/",
            "reason": "outside_allowed_domains",
            "stage": "url_admission",
        }
    ]


def test_long_search_snippet_is_queued_for_scrape_not_used_as_page_evidence():
    class Retriever:
        def __init__(self, _query, query_domains=None):
            self.query_domains = query_domains

        def search(self, max_results=10):
            return [
                {
                    "href": REQUIRED[0]["url"],
                    "title": "Search title",
                    "body": "Search snippet " * 100,
                }
            ]

    researcher = SimpleNamespace(
        source_policy=strict_policy(),
        source_rejections=[],
        visited_urls=set(),
        verbose=False,
        retrievers=[Retriever],
        cfg=SimpleNamespace(max_search_results_per_query=5),
        add_research_sources=lambda _sources: (_ for _ in ()).throw(
            AssertionError("snippet must not be persisted as fetched evidence")
        ),
        add_source_rejection=lambda *_args, **_kwargs: None,
    )
    conductor = ResearchConductor(researcher)

    urls, prefetched = asyncio.run(
        conductor._search_relevant_source_urls("nurse residency", [])
    )

    assert urls == [REQUIRED[0]["url"]]
    assert prefetched == []


def test_strict_api_full_text_is_queued_through_firecrawl():
    class Retriever:
        def __init__(self, _query, query_domains=None):
            self.query_domains = query_domains

        def search(self, max_results=10):
            return [
                {
                    "href": REQUIRED[0]["url"],
                    "title": "API-fetched authority",
                    "body": "Authoritative API text " * 20,
                    "raw_content": "Authoritative API text " * 20,
                }
            ]

    persisted = []
    researcher = SimpleNamespace(
        source_policy=strict_policy(),
        source_rejections=[],
        visited_urls=set(),
        verbose=False,
        retrievers=[Retriever],
        cfg=SimpleNamespace(max_search_results_per_query=5),
        add_research_sources=lambda sources: persisted.extend(sources),
        add_source_rejection=lambda *_args, **_kwargs: None,
    )
    conductor = ResearchConductor(researcher)

    urls, prefetched = asyncio.run(
        conductor._search_relevant_source_urls("nurse residency", [])
    )

    assert urls == [REQUIRED[0]["url"]]
    assert prefetched == []
    assert persisted == []


def test_advisory_api_full_text_is_prefetched_without_page_scrape():
    class Retriever:
        def __init__(self, _query, query_domains=None):
            self.query_domains = query_domains

        def search(self, max_results=10):
            return [
                {
                    "href": REQUIRED[0]["url"],
                    "title": "API-fetched authority",
                    "body": "Authoritative API text " * 20,
                    "raw_content": "Authoritative API text " * 20,
                }
            ]

    persisted = []
    researcher = SimpleNamespace(
        source_policy=SourcePolicy(),
        source_rejections=[],
        visited_urls=set(),
        verbose=False,
        retrievers=[Retriever],
        cfg=SimpleNamespace(max_search_results_per_query=5),
        add_research_sources=lambda sources: persisted.extend(sources),
        add_source_rejection=lambda *_args, **_kwargs: None,
    )
    conductor = ResearchConductor(researcher)

    urls, prefetched = asyncio.run(
        conductor._search_relevant_source_urls("nurse residency", [])
    )

    assert urls == []
    assert prefetched == persisted
    assert prefetched[0]["title"] == "API-fetched authority"
