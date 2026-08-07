from types import SimpleNamespace

import pytest

import gpt_researcher.skills.writer as writer_module
from gpt_researcher.skills.writer import (
    ReportGenerator,
    code_teammate_audit_prompt,
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


def test_code_report_context_keeps_multiple_opened_files_in_view():
    sources = [
        {
            "tool_name": "read_source",
            "title": f"workflow stage {index}",
            "url": (
                "https://github.com/Awhitter/repo/blob/"
                + str(index) * 40
                + f"/stage-{index}.ts#L1-L200"
            ),
            "content": f"STAGE_{index}\n" + (chr(64 + index) * 20_000),
        }
        for index in range(1, 7)
    ]

    compacted = compact_report_context(
        "BROAD_SEARCH_CONTEXT",
        sources,
        max_chars=50_000,
        opened_sources_only=True,
    )

    assert all(f"STAGE_{index}" in compacted for index in range(1, 7))
    assert "BROAD_SEARCH_CONTEXT" not in compacted


def test_code_report_context_keeps_eight_bounded_opened_files_in_view():
    sources = [
        {
            "tool_name": "read_source",
            "title": f"workflow stage {index}",
            "url": (
                "https://github.com/Awhitter/repo/blob/"
                + str(index) * 40
                + f"/stage-{index}.ts#L1-L200"
            ),
            "content": f"STAGE_{index}\n" + (chr(64 + index) * 20_000),
        }
        for index in range(1, 9)
    ]

    compacted = compact_report_context(
        "BROAD_SEARCH_CONTEXT",
        sources,
        max_chars=50_000,
        opened_sources_only=True,
    )

    assert all(f"STAGE_{index}" in compacted for index in range(1, 9))
    assert "BROAD_SEARCH_CONTEXT" not in compacted


def test_code_report_context_keeps_ten_question_sources_in_view():
    sources = [
        {
            "tool_name": "read_source",
            "title": f"question evidence {index}",
            "url": (
                "https://github.com/Awhitter/repo/blob/"
                + f"{index:x}" * 40
                + f"/question-{index}.ts#L1-L200"
            ),
            "content": f"QUESTION_{index}\n" + (chr(64 + index) * 20_000),
        }
        for index in range(1, 11)
    ]

    compacted = compact_report_context(
        "BROAD_SEARCH_CONTEXT",
        sources,
        max_chars=50_000,
        opened_sources_only=True,
    )

    assert all(f"QUESTION_{index}" in compacted for index in range(1, 11))
    assert "BROAD_SEARCH_CONTEXT" not in compacted


def test_code_report_context_keeps_late_workflow_sources_in_view():
    sources = [
        {
            "tool_name": "read_source",
            "title": f"opened workflow evidence {index}",
            "url": (
                "https://github.com/Awhitter/repo/blob/"
                + f"{index:x}" * 40
                + f"/workflow-{index}.ts#L1-L200"
            ),
            "content": f"WORKFLOW_{index}_EVIDENCE\n" + (chr(64 + index) * 20_000),
        }
        for index in range(1, 18)
    ]

    compacted = compact_report_context(
        "BROAD_SEARCH_CONTEXT",
        sources,
        max_chars=50_000,
        opened_sources_only=True,
    )

    assert len(compacted) <= 50_000
    assert all(f"WORKFLOW_{index}_EVIDENCE" in compacted for index in range(1, 18))
    assert "BROAD_SEARCH_CONTEXT" not in compacted


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
    assert "model validation" in prompt
    assert "not by itself proof" in prompt
    assert "not verified in the opened sources for this run" in prompt
    assert "silently audit every use" in prompt
    assert "Never add likely, standard, illustrative, or inferred fields" in prompt
    assert "scan every opened-file block" in prompt
    assert "implemented path but not live runtime use" in prompt
    assert "not verified" in prompt
    assert "What attributes do we capture?" in prompt


def test_opened_source_context_labels_interface_and_ui_evidence_limits():
    source = {
        "tool_name": "read_source",
        "title": "campaign dashboard",
        "url": (
            "https://github.com/Awhitter/katailyst2/blob/"
            + "d" * 40
            + "/app/(dashboard)/content/campaigns/page.tsx#L1-L30"
        ),
        "content": "interface LifecycleSignals { email?: string }",
    }

    compacted = compact_report_context(
        "search noise",
        [source],
        max_chars=20_000,
        opened_sources_only=True,
    )

    assert "presentation or caller surface" in compacted
    assert "declared interface or type proves a data shape" in compacted
    assert "repository location alone does not prove" in compacted.lower()


def test_code_teammate_audit_forbids_global_marketo_and_interface_leaps():
    prompt = code_teammate_audit_prompt(
        "Who owns these fields, and do we send them to Marketo?",
        "An interface is in K2, so K2 owns them and Marketo is not used.",
    )

    assert "Never infer capture, persistence, ownership" in prompt
    assert "inactive Marketo pilot" in prompt
    assert "Absence is local to this evidence set" in prompt
    assert "Delete speculative implementation-location phrases" in prompt
    assert "Never say evidence was not opened" in prompt
    assert "implemented code from live-runtime readback" in prompt


@pytest.mark.asyncio
async def test_code_only_writer_automatically_uses_teammate_contract(monkeypatch):
    captured = []

    async def fake_generate_report(**kwargs):
        captured.append(kwargs)
        return "draft answer" if len(captured) == 1 else "audited grounded answer"

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

    assert report == "audited grounded answer"
    assert len(captured) == 2
    assert "Lead with the answer" in captured[0]["custom_prompt"]
    assert "How does this route work?" in captured[0]["custom_prompt"]
    assert "Audit and rewrite the draft" in captured[1]["custom_prompt"]
    assert "draft answer" in captured[1]["custom_prompt"]
    assert captured[0]["websocket"] is None
    assert captured[1]["websocket"] is None
    assert "OPENED_ROUTE_EVIDENCE" in captured[0]["context"]
    assert "BROAD_SEARCH_CONTEXT" not in captured[0]["context"]
