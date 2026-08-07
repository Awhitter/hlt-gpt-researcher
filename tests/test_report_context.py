from types import SimpleNamespace

import pytest

import gpt_researcher.skills.writer as writer_module
from gpt_researcher.skills.writer import (
    ReportGenerator,
    code_teammate_report_prompt,
    compact_report_context,
)


def test_large_report_context_prioritizes_opened_files_and_stays_bounded():
    source = {
        "tool_name": "read_source",
        "title": "identity route",
        "url": "https://github.com/Awhitter/repo/blob/" + "a" * 40 + "/identity.ts",
        "content": "EXACT_FILE_EVIDENCE\n" + "field = value\n" * 900,
    }
    context = "BROAD_SEARCH_NOISE\n" + "noise\n" * 20_000

    compacted = compact_report_context(context, [source], max_chars=20_000)

    assert len(compacted) <= 20_000
    assert "EXACT_FILE_EVIDENCE" in compacted
    assert compacted.index("EXACT_FILE_EVIDENCE") < compacted.find("BROAD_SEARCH_NOISE") or (
        "BROAD_SEARCH_NOISE" not in compacted
    )


def test_small_report_context_is_left_intact():
    context = "small, already useful context"

    assert compact_report_context(context, [SimpleNamespace()], max_chars=20_000) == context


def test_report_context_override_has_a_sane_upper_bound():
    context = "x" * 200_000

    compacted = compact_report_context(context, [], max_chars=1_000_000)

    assert 0 < len(compacted) <= 60_000


def test_list_context_keeps_opened_chunk_boundaries():
    context = ["FIRST_CHUNK\n" + "a" * 12_000, "SECOND_CHUNK\n" + "b" * 12_000]

    compacted = compact_report_context(context, [], max_chars=20_000)

    assert "FIRST_CHUNK" in compacted
    assert "SECOND_CHUNK" in compacted


def test_code_report_context_uses_opened_files_not_broad_search_results():
    sources = [
        {
            "tool_name": "search_source",
            "url": "https://example.test/search",
            "content": "SEARCH_RESULT_THAT_MAY_ONLY_HINT_AT_THE_ANSWER",
        },
        {
            "tool_name": "read_source",
            "title": "account schema",
            "url": "https://github.com/Awhitter/repo/blob/" + "b" * 40 + "/account.ts#L10-L30",
            "content": "OPENED_IMPLEMENTATION_EVIDENCE",
        },
        {
            "tool_name": "verify_source_ref",
            "url": "https://github.com/Awhitter/repo/blob/" + "b" * 40 + "/account.ts#L10-L30",
            "content": "VERIFICATION_METADATA_IS_NOT_CLAIM_EVIDENCE",
        },
    ]

    compacted = compact_report_context(
        "BROAD_SEARCH_CONTEXT",
        sources,
        max_chars=20_000,
        opened_sources_only=True,
    )

    assert "OPENED_IMPLEMENTATION_EVIDENCE" in compacted
    assert "BROAD_SEARCH_CONTEXT" not in compacted
    assert "SEARCH_RESULT_THAT_MAY_ONLY_HINT_AT_THE_ANSWER" not in compacted
    assert "VERIFICATION_METADATA_IS_NOT_CLAIM_EVIDENCE" not in compacted


def test_code_report_without_opened_file_states_evidence_is_missing():
    compacted = compact_report_context(
        "A broad result that sounds plausible",
        [],
        opened_sources_only=True,
    )

    assert "No repository file was opened" in compacted
    assert "sounds plausible" not in compacted


def test_code_teammate_prompt_prevents_inference_and_authority_flattening():
    prompt = code_teammate_report_prompt("What attributes do we capture?")

    assert "Lead with the answer" in prompt
    assert "250-700 words" in prompt
    assert "client interface is not proof of data ownership" in prompt
    assert "Never add likely, standard, illustrative, or inferred fields" in prompt
    assert "not verified" in prompt
    assert "What attributes do we capture?" in prompt


@pytest.mark.asyncio
async def test_code_only_writer_automatically_uses_teammate_contract(monkeypatch):
    captured = {}

    async def fake_generate_report(**kwargs):
        captured.update(kwargs)
        return "grounded answer"

    monkeypatch.setattr(writer_module, "generate_report", fake_generate_report)
    researcher = SimpleNamespace(
        query="How does this route work?",
        cfg=SimpleNamespace(agent_role="", total_words=1_000),
        role="research teammate",
        report_type="research_report",
        report_source="mcp",
        tone=SimpleNamespace(value="objective"),
        websocket=None,
        headers={},
        context="BROAD_SEARCH_CONTEXT",
        mcp_only=True,
        verbose=False,
        kwargs={},
        get_research_images=lambda: [],
        get_research_sources=lambda: [
            {
                "tool_name": "read_source",
                "url": "https://github.com/Awhitter/repo/blob/" + "c" * 40 + "/route.ts#L1-L20",
                "content": "OPENED_ROUTE_EVIDENCE",
            }
        ],
        add_costs=lambda *_args, **_kwargs: None,
    )

    report = await ReportGenerator(researcher).write_report()

    assert report == "grounded answer"
    assert "Lead with the answer" in captured["custom_prompt"]
    assert "How does this route work?" in captured["custom_prompt"]
    assert "OPENED_ROUTE_EVIDENCE" in captured["context"]
    assert "BROAD_SEARCH_CONTEXT" not in captured["context"]
