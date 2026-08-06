"""The suggestion bank behind "try one of these" in the UI and in Slack.

The tool can already answer "how does ScraperVault hand applications to
nursing-mastery?" — nobody knows to ask. The old fix was 13 hardcoded chips
(`lib/starterPrompts.ts`), which went stale and then died entirely.

So the bank is built by filling **curated templates with live nouns**. The
templates are written by hand, so nothing is hallucinated; the nouns come from
sources that already refresh themselves, so nothing goes stale:

* Linear shipped issues — 21-day window, 5-minute cache
* ``my-docs/recruiting/content-inventory.md`` — regenerated weekly by the
  audience-sweep cron
* ``my-docs/audience/pain-points.md`` — re-ranked by the same weekly sweep
* ``my-docs/vision/capabilities.md`` — the "Team questions it answers" column,
  used verbatim
* codegraph repo cards — live repo names and index freshness

Every entry pins its scope. That matters for honesty, not just routing:
explicit scope beats Auto in ``prepare_research_request``, so a pinned
suggestion bypasses inference entirely and "this one searches the code" is true
by construction. Auto's own inference is regex-scored with an LLM tiebreak and
can silently drop a scope when an integration is down, so it could not be
labelled ahead of time.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Iterable

_CACHE_TTL_SECONDS = 300
_cache: tuple[float, list[dict[str, Any]]] | None = None

# `## pay (293 pages)`
_INVENTORY_SECTION = re.compile(r"^##\s+([a-z0-9][a-z0-9-]*)\s+\((\d+)\s+pages?\)", re.M)
# A markdown table row, split into cells.
_TABLE_ROW = re.compile(r"^\|(.+)\|\s*$", re.M)
# Curly or straight quoted questions in the capabilities table.
_QUOTED = re.compile(r"[“\"]([^”\"]{8,120})[”\"]")


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _entry(
    key: str, label: str, prompt: str, scope: dict[str, Any], category: str, source: str
) -> dict[str, Any]:
    return {
        "id": key,
        "label": label,
        "prompt": prompt,
        "scope": scope,
        "category": category,
        "source": source,
    }


def _slug(text: str, limit: int = 48) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or "x"


# Chips sit three-to-a-row under the ask box. Past this they wrap awkwardly or
# clip mid-word; the full question still goes in the prompt and the tooltip.
CHIP_LABEL_MAX = 38


def chip_label(text: str, limit: int = CHIP_LABEL_MAX) -> str:
    """Trim to a word boundary with an ellipsis, never mid-word."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return f"{cut.rstrip(' ,.;:—-')}…"


def from_repos(repos: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repo-grounded questions: each repo's own examples, plus handoffs.

    Only repos whose index is usable are offered — suggesting a question we
    cannot currently answer is worse than suggesting nothing.
    """
    usable = [r for r in repos if r.get("status") in {"ready", "partial"}]
    out: list[dict[str, Any]] = []

    for repo in usable:
        name = repo.get("name") or repo.get("slug")
        for example in repo.get("ask_examples") or []:
            out.append(
                _entry(
                    f"repo:{repo.get('slug')}:{_slug(example)}",
                    chip_label(example),
                    f"Regarding {name} ({repo.get('github')}): {example}",
                    {"codebase": True},
                    "code",
                    "repo-card",
                )
            )

    # Handoff questions are the ones a PM actually asks and the code graph is
    # unusually good at. Ordered pairs, one direction each, to keep the bank
    # from filling up with near-duplicates.
    for i, left in enumerate(usable):
        for right in usable[i + 1 :]:
            a, b = left.get("name"), right.get("name")
            out.append(
                _entry(
                    f"handoff:{left.get('slug')}:{right.get('slug')}",
                    chip_label(f"{a} → {b}"),
                    f"How does {a} hand off to {b}? Trace the actual call path "
                    f"or data flow, and name the files involved.",
                    {"codebase": True, "depth": "deep"},
                    "code",
                    "repo-card",
                )
            )
    return out


def from_shipped(shipped: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in shipped:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        out.append(
            _entry(
                f"shipped:{_slug(item.get('id') or title)}",
                chip_label(f"What changed: {title}"),
                f'We shipped "{title}". What actually changed in the code, and '
                f"what should the team know about it?",
                {"codebase": True},
                "shipped",
                "linear",
            )
        )
    return out


def from_content_inventory(markdown: str, min_pages: int = 5) -> list[dict[str, Any]]:
    """Only sections with real coverage.

    The inventory has a long tail of 1-page sections; "where are the gaps in
    our for-employers content (1 page)" is a worse question than no question.
    """
    out: list[dict[str, Any]] = []
    for section, pages in _INVENTORY_SECTION.findall(markdown):
        if int(pages) < min_pages:
            continue
        out.append(
            _entry(
                f"content-gap:{section}",
                f"Gaps in {section}",
                f"We have {pages} pages under /{section} on nursingmastery.com. "
                f"Where are the gaps compared with the best nurse-facing content "
                f"anywhere, and what should we write next?",
                {"recruiting": True, "firecrawl": True, "depth": "deep"},
                "content",
                "content-inventory",
            )
        )
    return out


def from_pain_points(markdown: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in _TABLE_ROW.findall(markdown):
        cells = _cells(row)
        if len(cells) < 3 or not cells[0].isdigit():
            continue  # header, separator, or prose table
        rank, pain, who = cells[0], cells[1], cells[2]
        out.append(
            _entry(
                f"pain:{rank}:{_slug(pain)}",
                chip_label(pain),
                f'What do nurses actually say about "{pain}"? Focus on {who}. '
                f"Quote real posts with links, and say how common it is.",
                {"audience": True, "firecrawl": True, "depth": "deep"},
                "nurses",
                "pain-points",
            )
        )
    return out


def from_capabilities(markdown: str) -> list[dict[str, Any]]:
    """The capabilities table's third column is literally a list of questions."""
    out: list[dict[str, Any]] = []
    for row in _TABLE_ROW.findall(markdown):
        cells = _cells(row)
        if len(cells) < 3:
            continue
        repo = cells[0].strip("* ")
        for question in _QUOTED.findall(cells[2]):
            out.append(
                _entry(
                    f"capability:{_slug(repo)}:{_slug(question)}",
                    chip_label(question),
                    f"{question} (about {repo})",
                    {"codebase": True},
                    "code",
                    "capabilities",
                )
            )
    return out


# Hand-written entries that no live source produces. Carried over from the
# starter chips that went dead in a72d6a50 — they were good, they were just
# static and unreachable.
CURATED: tuple[dict[str, Any], ...] = (
    _entry(
        "curated:best-on-earth",
        "Best on earth",
        "Find the best nurse-recruiting content and product experiences anywhere "
        "on earth. Distill why each one works, then propose how the mechanism "
        "maps to Nursing Mastery.",
        {"recruiting": True, "audience": True, "mode": "top1", "depth": "deep"},
        "web",
        "curated",
    ),
    _entry(
        "curated:nurse-trends",
        "Trending with nurses",
        "What are nurses and nursing students talking about most this month? "
        "Use forums and quote real posts with links.",
        {"audience": True, "firecrawl": True, "depth": "deep"},
        "nurses",
        "curated",
    ),
    _entry(
        "curated:can-we-do-x",
        "Can we build this?",
        "Can we build a saved-search email alert for nurses? Check what already "
        "exists across the estate before proposing anything new.",
        {"codebase": True, "cms": True},
        "code",
        "curated",
    ),
    _entry(
        "curated:registry-skill",
        "Is there a skill for this?",
        "Is there already a Katailyst2 skill or playbook for this, or would we "
        "be building a duplicate?",
        {"cms": True},
        "code",
        "curated",
    ),
)


def build_suggestions(
    *,
    repos: Iterable[dict[str, Any]] = (),
    shipped: Iterable[dict[str, Any]] = (),
    content_inventory: str = "",
    pain_points: str = "",
    capabilities: str = "",
) -> list[dict[str, Any]]:
    """Assemble the bank. Pure, deterministic, and degrades to CURATED alone."""
    bank: list[dict[str, Any]] = []
    bank.extend(from_repos(repos))
    bank.extend(from_shipped(shipped))
    bank.extend(from_content_inventory(content_inventory))
    bank.extend(from_pain_points(pain_points))
    bank.extend(from_capabilities(capabilities))
    bank.extend(CURATED)

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in bank:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        unique.append(item)

    return _interleave_by_category(unique)


def _interleave_by_category(bank: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Round-robin the categories so any window of three is varied.

    The content inventory alone contributes far more entries than any other
    source, so a naive order would show three "content gap" chips at once. The
    client just takes a sliding window, which keeps it dumb and keeps the order
    deterministic — SSR and hydration have to agree.
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in bank:
        buckets.setdefault(item["category"], []).append(item)

    order = sorted(buckets, key=lambda c: (-len(buckets[c]), c))
    out: list[dict[str, Any]] = []
    while any(buckets[c] for c in order):
        for category in order:
            if buckets[category]:
                out.append(buckets[category].pop(0))
    return out


def _corpus_text(loader: Callable[[str], list[dict[str, Any]]], subdir: str, filename: str) -> str:
    for doc in loader(subdir):
        if doc.get("filename") == filename:
            return doc.get("content") or ""
    return ""


def get_brain_suggestions(
    *,
    repos_fn: Callable[[], list[dict[str, Any]]],
    shipped_fn: Callable[[], list[dict[str, Any]] | None],
    corpus_fn: Callable[[str], list[dict[str, Any]]],
    now: float | None = None,
) -> dict[str, Any]:
    """Cached bank for ``GET /api/brain/suggestions``.

    Dependencies are injected rather than imported so this leaf never reaches
    back into the router, and so the tests can run it without Linear or a disk.
    """
    global _cache
    stamp = now if now is not None else time.time()
    if _cache and stamp - _cache[0] < _CACHE_TTL_SECONDS:
        return {"suggestions": _cache[1], "cached": True}

    def _safe(fn, fallback):
        try:
            return fn() or fallback
        except Exception:  # noqa: BLE001 - a suggestion strip must never 500
            return fallback

    bank = build_suggestions(
        repos=_safe(repos_fn, []),
        shipped=_safe(shipped_fn, []),
        content_inventory=_corpus_text(corpus_fn, "recruiting", "content-inventory.md"),
        pain_points=_corpus_text(corpus_fn, "audience", "pain-points.md"),
        capabilities=_corpus_text(corpus_fn, "vision", "capabilities.md"),
    )
    _cache = (stamp, bank)
    return {"suggestions": bank, "cached": False}


def reset_cache() -> None:
    global _cache
    _cache = None
