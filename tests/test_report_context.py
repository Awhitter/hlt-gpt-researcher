from types import SimpleNamespace

from gpt_researcher.skills.writer import compact_report_context


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
