"""Shared text helpers for HLT term matching.

Media asset ranking and research-library search both score free text against
a query, so the stopword list and tokenizer live here instead of being
duplicated or borrowed across sibling modules.
"""
from __future__ import annotations

import re

STOPWORDS = {
    "about",
    "across",
    "after",
    "also",
    "and",
    "are",
    "could",
    "find",
    "for",
    "from",
    "have",
    "help",
    "into",
    "latest",
    "like",
    "make",
    "that",
    "the",
    "this",
    "through",
    "what",
    "when",
    "with",
    "would",
    "your",
}


def task_terms(task: str, *, limit: int = 12) -> list[str]:
    """Ordered, de-duplicated significant terms from a task string."""

    seen: set[str] = set()
    terms: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", task.lower()):
        term = raw.strip("_-")
        if len(term) < 3 or term in STOPWORDS or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= limit:
            break
    return terms
