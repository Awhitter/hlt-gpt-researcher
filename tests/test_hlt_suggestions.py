"""Tests for the suggestion bank.

The bank exists because the tool could already answer estate questions and
nobody knew to ask. Its predecessor — 13 hardcoded chips — went stale and then
died. So the properties that matter are: it stays fresh (built from live
sources), it never lies (every entry pins a real scope), it degrades instead of
failing, and it stays deterministic (the client shuffles a window; SSR and
hydration must agree).
"""
from __future__ import annotations

import pytest

from backend.server import hlt_suggestions as hs

# The scope keys the frontend's HLTResearchScope actually carries. A typo here
# would silently produce a chip that pins nothing and quietly falls back to Auto.
VALID_SCOPE_KEYS = {
    "auto", "codebase", "cms", "qbank", "metrics",
    "firecrawl", "media", "audience", "recruiting", "depth", "mode",
}
VALID_DEPTHS = {"fast", "balanced", "deep"}
VALID_MODES = {"standard", "top1"}

CONTENT_INVENTORY = """# Inventory
## pay (293 pages)
stuff
## community (59 pages)
stuff
## for-employers (1 pages)
stuff
"""

PAIN_POINTS = """# Pain points

| Rank | Pain | Who feels it | Evidence status |
| --- | --- | --- | --- |
| 1 | Getting the first job without connections | New grads | needs quotes |
| 2 | Orientation ends too early | First-year RNs | needs quotes |
"""

CAPABILITIES = """# Estate capability map

| Repo | One-liner | Team questions it answers |
|------|-----------|---------------------------|
| **nursing-mastery** | Nurse-facing home | “What does the nurse see?” “Where does apply live?” |
| **ScraperVault** | Recruiting backend | “Do we store applications?” |
"""

REPOS = [
    {"slug": "nursing-mastery", "name": "nursing-mastery", "github": "Awhitter/nursing-mastery",
     "status": "ready", "ask_examples": ["Where does the nurse apply flow live?"]},
    {"slug": "scrapervault", "name": "ScraperVault", "github": "Awhitter/ScraperVault",
     "status": "ready", "ask_examples": []},
    {"slug": "mmm2", "name": "MMM2", "github": "Awhitter/MMM2",
     "status": "unavailable", "ask_examples": ["Can we generate video?"]},
]


@pytest.fixture(autouse=True)
def _clear_cache():
    hs.reset_cache()
    yield
    hs.reset_cache()


def _full_bank():
    return hs.build_suggestions(
        repos=REPOS,
        shipped=[{"id": "linear-KAT-1", "title": "Best for you ranking"}],
        content_inventory=CONTENT_INVENTORY,
        pain_points=PAIN_POINTS,
        capabilities=CAPABILITIES,
    )


# --- honesty ----------------------------------------------------------------


def test_every_entry_pins_a_real_scope():
    """A pinned scope is what makes a chip's label true.

    Explicit scope beats Auto in prepare_research_request, so a pinned chip
    bypasses inference entirely — but only if the keys are ones the backend
    recognises.
    """
    for item in _full_bank():
        assert item["scope"], f"{item['id']} pins nothing and would silently use Auto"
        unknown = set(item["scope"]) - VALID_SCOPE_KEYS
        assert not unknown, f"{item['id']} pins unknown scope keys {unknown}"
        if "depth" in item["scope"]:
            assert item["scope"]["depth"] in VALID_DEPTHS
        if "mode" in item["scope"]:
            assert item["scope"]["mode"] in VALID_MODES


def test_entries_are_well_formed():
    for item in _full_bank():
        assert item["label"] and item["prompt"]
        assert len(item["label"]) <= hs.CHIP_LABEL_MAX + 1, "chip labels must stay chip-sized"
        # The prompt is what actually runs; it should be a real question, not
        # the truncated chip text.
        assert len(item["prompt"]) >= len(item["label"])


def test_long_labels_are_trimmed_at_a_word_boundary():
    """A chip that clips mid-word ("...a \"big name\" sc") reads as a bug."""
    long = "Getting the first job without connections or a big name school"
    label = hs.chip_label(long)

    assert label.endswith("…")
    assert len(label) <= hs.CHIP_LABEL_MAX + 1
    assert not label[:-1].endswith(" "), "no dangling space before the ellipsis"
    # The trimmed text must be whole words from the original.
    assert long.startswith(label[:-1])
    assert label[:-1].split()[-1] in long.split()


def test_short_labels_are_untouched():
    assert hs.chip_label("What does the nurse see?") == "What does the nurse see?"


def test_the_full_question_survives_in_the_prompt():
    """Trimming is presentation only — the run must use the whole question."""
    bank = hs.from_pain_points(PAIN_POINTS)
    assert "Getting the first job without connections" in bank[0]["prompt"]


def test_ids_are_unique():
    ids = [i["id"] for i in _full_bank()]
    assert len(ids) == len(set(ids))


def test_repos_we_cannot_search_are_not_offered():
    """Suggesting a question we'd fail to answer is worse than suggesting none."""
    bank = hs.from_repos(REPOS)
    assert not any("mmm2" in i["id"] for i in bank), "unavailable repo was offered"
    assert any("nursing-mastery" in i["id"] for i in bank)


# --- freshness --------------------------------------------------------------


def test_content_gaps_skip_the_long_tail():
    bank = hs.from_content_inventory(CONTENT_INVENTORY)
    labels = " ".join(i["label"] for i in bank)
    assert "pay" in labels and "community" in labels
    assert "for-employers" not in labels, "a 1-page section is not a content gap"


def test_pain_points_parse_rows_not_headers():
    bank = hs.from_pain_points(PAIN_POINTS)
    assert len(bank) == 2
    assert "first job" in bank[0]["prompt"]
    assert "New grads" in bank[0]["prompt"], "the 'who' column should reach the prompt"


def test_capabilities_uses_the_questions_column_verbatim():
    bank = hs.from_capabilities(CAPABILITIES)
    labels = {i["label"] for i in bank}
    assert "What does the nurse see?" in labels
    assert "Where does apply live?" in labels
    assert "Do we store applications?" in labels


def test_shipped_work_becomes_a_question():
    bank = hs.from_shipped([{"id": "linear-KAT-1", "title": "Best for you ranking"}])
    assert len(bank) == 1
    assert "Best for you ranking" in bank[0]["prompt"]
    assert bank[0]["scope"] == {"codebase": True}


def test_handoff_pairs_are_generated_once_per_pair():
    bank = [i for i in hs.from_repos(REPOS) if i["id"].startswith("handoff:")]
    # Two usable repos -> exactly one ordered pair, not two.
    assert len(bank) == 1


# --- resilience -------------------------------------------------------------


def test_degrades_to_curated_when_every_source_is_empty():
    bank = hs.build_suggestions()
    assert len(bank) == len(hs.CURATED)
    assert all(i["source"] == "curated" for i in bank)


def test_a_failing_source_does_not_take_the_endpoint_down():
    def boom():
        raise RuntimeError("linear is down")

    result = hs.get_brain_suggestions(
        repos_fn=boom, shipped_fn=boom, corpus_fn=lambda _s: [], now=1000.0
    )
    assert result["suggestions"], "a dead source should not empty the bank"


# --- determinism and shape --------------------------------------------------


def test_order_is_deterministic():
    assert [i["id"] for i in _full_bank()] == [i["id"] for i in _full_bank()]


def test_consecutive_entries_vary_by_category():
    """The client shows a sliding window of three; three content-gap chips in a
    row would read as though that is all the tool does."""
    bank = _full_bank()
    categories = [i["category"] for i in bank]
    assert len(set(categories)) > 1

    for i in range(len(categories) - 2):
        window = categories[i : i + 3]
        # Once the smaller buckets are exhausted the tail is necessarily
        # single-category; only the interleaved head has to be varied.
        if i + 3 <= len(set(categories)) * 2:
            assert len(set(window)) > 1, f"window at {i} is all {window[0]}"


def test_cache_is_used_and_resettable():
    calls = {"n": 0}

    def repos():
        calls["n"] += 1
        return []

    first = hs.get_brain_suggestions(
        repos_fn=repos, shipped_fn=lambda: [], corpus_fn=lambda _s: [], now=1000.0
    )
    second = hs.get_brain_suggestions(
        repos_fn=repos, shipped_fn=lambda: [], corpus_fn=lambda _s: [], now=1010.0
    )
    assert first["cached"] is False and second["cached"] is True
    assert calls["n"] == 1

    third = hs.get_brain_suggestions(
        repos_fn=repos, shipped_fn=lambda: [], corpus_fn=lambda _s: [], now=9999.0
    )
    assert third["cached"] is False and calls["n"] == 2
