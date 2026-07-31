"""Estate context for the Brain tabs: repo cards, corpora, library, Linear.

Read-only helpers behind `/api/brain/*`. Kept separate from the research
request pipeline so the router module stays about routing.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any
import urllib.error
import urllib.request

from .hlt_text import STOPWORDS

logger = logging.getLogger(__name__)

_BRAIN_REPO_CARDS: tuple[dict[str, Any], ...] = (
    {
        "slug": "nursing-mastery",
        "github": "Awhitter/nursing-mastery",
        "name": "Nursing Mastery",
        "tagline": "Nurse-facing career home and product surface",
        "capabilities": [
            "Nurse career experience and content surfaces",
            "Apply / recruiting UX that sits on ScraperVault data",
            "Brand-facing product home for Nursing Mastery",
        ],
        "ask_examples": [
            "Where does the nurse apply flow live?",
            "What pages are public vs authenticated?",
        ],
    },
    {
        "slug": "scrapervault",
        "github": "Awhitter/ScraperVault",
        "name": "ScraperVault",
        "tagline": "Nurse-recruiting backend — jobs, employers, people, applications",
        "capabilities": [
            "Jobs, employers, people, and applications data",
            "Recruiting pipelines and semantic layers",
            "Source-of-truth for hiring operations",
        ],
        "ask_examples": [
            "Can we filter employers by specialty?",
            "Where are applications stored?",
        ],
    },
    {
        "slug": "katailyst2",
        "github": "Awhitter/katailyst2",
        "name": "Katailyst2",
        "tagline": "AI primitives, registry, and creation / command hub",
        "capabilities": [
            "Entity registry (skills, prompts, playbooks, KBs)",
            "MCP tool surface for agents",
            "Orchestration and discovery for the estate",
        ],
        "ask_examples": [
            "Is there already a skill for competitor research?",
            "How do agents discover tools?",
        ],
    },
    {
        "slug": "mmm2",
        "github": "Awhitter/MMM2",
        "name": "MMM2",
        "tagline": "Multimedia Maker — images, video, TTS (Cloudinary-primary)",
        "capabilities": [
            "Image / video / TTS generation pipelines",
            "Cloudinary upload and media library integration",
            "Media APIs consumed by other HLT surfaces",
        ],
        "ask_examples": [
            "Can MMM2 generate short recruiter explainers?",
            "Which image models are wired?",
        ],
    },
    {
        "slug": "ebb",
        "github": "Awhitter/evidence-based-business",
        "name": "EBB",
        "tagline": "Metrics and analytics layer",
        "capabilities": [
            "Business metrics and dashboards",
            "Evidence-backed reporting for product decisions",
            "Analytics primitives for Nursing Mastery / recruiting",
        ],
        "ask_examples": [
            "What conversion metrics do we track?",
            "Where do Metabase questions live?",
        ],
    },
)


def get_brain_repos(*, codegraph_ready: bool = False) -> list[dict[str, Any]]:
    """Estate repo cards for the Codebase explorer tab.

    Readiness is passed in rather than looked up so this module stays a leaf
    of the request pipeline instead of importing back into it.
    """
    return [
        {
            **card,
            "codegraph_ready": codegraph_ready,
        }
        for card in _BRAIN_REPO_CARDS
    ]


def _load_corpus_documents(subdir: str) -> list[dict[str, Any]]:
    """Load a markdown corpus from DOC_PATH/<subdir> (repo docs/<subdir> fallback).

    Corpora under DOC_PATH double as hybrid-research context: DocumentLoader
    walks the whole DOC_PATH tree, so anything surfaced here is also what the
    researcher reads in `hybrid`/`local` report sources.
    """
    candidates = [
        os.path.join(os.getenv("DOC_PATH", "./my-docs"), subdir),
        os.path.join("docs", subdir),
        os.path.join(os.path.dirname(__file__), "..", "..", "docs", subdir),
    ]
    documents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for corpus_dir in candidates:
        corpus_dir = os.path.abspath(corpus_dir)
        if not os.path.isdir(corpus_dir):
            continue
        for name in sorted(os.listdir(corpus_dir)):
            if not name.endswith(".md") or name in seen:
                continue
            path = os.path.join(corpus_dir, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    content = handle.read()
            except OSError as exc:
                logger.warning("Failed to read %s doc %s: %s", subdir, path, exc)
                continue
            seen.add(name)
            documents.append(
                {
                    "id": name.removesuffix(".md"),
                    "filename": name,
                    "title": name.removesuffix(".md").replace("-", " ").title(),
                    "content": content,
                    "path": f"{subdir}/{name}",
                }
            )
    return documents


def _corpus_readiness(subdir: str) -> dict[str, Any]:
    docs = _load_corpus_documents(subdir)
    return {
        "status": "ready" if docs else "unavailable",
        "configured": bool(docs),
        "document_count": len(docs),
        "missing": [] if docs else [f"my-docs/{subdir}/*.md"],
    }


def get_brain_vision_documents() -> list[dict[str, Any]]:
    """Load vision markdown for the Vision tab + hybrid research."""
    return _load_corpus_documents("vision")


def get_brain_audience() -> dict[str, Any]:
    """Audience tab payload: voice-of-nurse corpus + recruiting content inventory."""
    audience_docs = _load_corpus_documents("audience")
    recruiting_docs = _load_corpus_documents("recruiting")
    return {
        "documents": audience_docs,
        "recruiting_documents": recruiting_docs,
        "note": (
            "Voice-of-nurse briefs, quote banks, and the nursingmastery.com "
            "content inventory. Every research run with the Audience or "
            "Recruiting scope reads these; the weekly sweep keeps them fresh."
        ),
    }


# --- Research library (memory layer over the report store) -------------------
#
# The frontend persists finished reports to REPORT_STORE_PATH via /api/reports.
# These read-only helpers make that archive searchable and let new research
# runs consult prior work so knowledge compounds instead of restarting.

_LIBRARY_STOPWORDS = STOPWORDS | {"does", "how", "why", "can", "should", "our", "will"}


def _report_store_entries() -> list[dict[str, Any]]:
    path = os.getenv("REPORT_STORE_PATH", os.path.join("data", "reports.json"))
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    return [entry for entry in data.values() if isinstance(entry, dict)]


def _library_terms(text: str) -> set[str]:
    return {
        term
        for term in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", text.lower())
        if term not in _LIBRARY_STOPWORDS
    }


def search_prior_reports(query: str, limit: int = 3) -> list[dict[str, Any]]:
    """Rank stored reports by keyword overlap with `query`. Cheap by design."""

    query_terms = _library_terms(query)
    if not query_terms:
        return []

    scored: list[tuple[float, dict[str, Any]]] = []
    for entry in _report_store_entries():
        question = str(entry.get("question") or "")
        answer = str(entry.get("answer") or "")
        if not question or not answer:
            continue
        question_terms = _library_terms(question)
        answer_terms = _library_terms(answer[:4000])
        question_overlap = len(query_terms & question_terms)
        answer_overlap = len(query_terms & answer_terms)
        score = question_overlap * 3 + answer_overlap
        if question_overlap == 0 and answer_overlap < 3:
            continue
        scored.append((score, entry))

    scored.sort(key=lambda pair: (-pair[0], -(pair[1].get("timestamp") or 0)))
    results = []
    for score, entry in scored[:limit]:
        timestamp = entry.get("timestamp")
        date = (
            time.strftime("%Y-%m-%d", time.gmtime(timestamp / 1000))
            if isinstance(timestamp, (int, float)) and timestamp > 0
            else None
        )
        answer = str(entry.get("answer") or "")
        results.append(
            {
                "id": entry.get("id"),
                "question": entry.get("question"),
                "date": date,
                "score": score,
                "snippet": re.sub(r"\s+", " ", answer)[:400],
            }
        )
    return results


def get_brain_library(query: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Library tab payload: the searchable archive of past research runs."""

    if query:
        matches = search_prior_reports(query, limit=min(limit, 25))
        return {"reports": matches, "query": query, "total": len(matches)}

    entries = _report_store_entries()
    entries.sort(key=lambda entry: -(entry.get("timestamp") or 0))
    reports = []
    for entry in entries[:limit]:
        timestamp = entry.get("timestamp")
        date = (
            time.strftime("%Y-%m-%d", time.gmtime(timestamp / 1000))
            if isinstance(timestamp, (int, float)) and timestamp > 0
            else None
        )
        answer = str(entry.get("answer") or "")
        reports.append(
            {
                "id": entry.get("id"),
                "question": entry.get("question"),
                "date": date,
                "snippet": re.sub(r"\s+", " ", answer)[:280],
            }
        )
    return {"reports": reports, "query": None, "total": len(entries)}


_LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
_LINEAR_CACHE_TTL_SECONDS = 300
_linear_cache: dict[str, tuple[float, Any]] = {}


def _linear_graphql(query: str, timeout: int = 8) -> dict[str, Any] | None:
    """Run a Linear GraphQL query with LINEAR_API_KEY. Returns None on any failure."""

    api_key = os.getenv("LINEAR_API_KEY")
    if not api_key:
        return None
    payload = json.dumps({"query": query}).encode("utf-8")
    request = urllib.request.Request(_LINEAR_GRAPHQL_URL, data=payload, method="POST")
    request.add_header("Authorization", api_key)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed Linear host
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        logger.warning("Linear GraphQL request failed: %s", type(error).__name__)
        return None
    if not isinstance(body, dict) or body.get("errors"):
        logger.warning("Linear GraphQL returned errors: %s", body.get("errors") if isinstance(body, dict) else "non-dict body")
        return None
    data = body.get("data")
    return data if isinstance(data, dict) else None


def _linear_cached(key: str, fetch) -> Any:
    now = time.time()
    cached = _linear_cache.get(key)
    if cached and now - cached[0] < _LINEAR_CACHE_TTL_SECONDS:
        return cached[1]
    result = fetch()
    if result is not None:
        _linear_cache[key] = (now, result)
    return result


def _fetch_linear_milestones() -> list[dict[str, Any]] | None:
    data = _linear_graphql(
        """
        query {
          projects(first: 25) {
            nodes {
              id
              name
              description
              state
              progress
              targetDate
              url
              updatedAt
            }
          }
        }
        """
    )
    if data is None:
        return None
    nodes = ((data.get("projects") or {}).get("nodes")) or []
    state_rank = {"started": 0, "planned": 1, "backlog": 2, "paused": 3, "completed": 4}
    milestones = []
    for node in nodes:
        state = node.get("state")
        if state == "canceled":
            continue
        milestones.append(
            {
                "id": node.get("id"),
                "title": node.get("name"),
                "summary": node.get("description") or "",
                "status": state,
                "progress": round(float(node.get("progress") or 0.0), 2),
                "target": node.get("targetDate"),
                "url": node.get("url"),
            }
        )
    milestones.sort(key=lambda item: state_rank.get(item["status"], 5))
    return milestones


def _fetch_linear_shipped() -> list[dict[str, Any]] | None:
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 21 * 86400))
    data = _linear_graphql(
        f"""
        query {{
          issues(
            first: 15
            filter: {{ completedAt: {{ gte: "{since}" }} }}
            orderBy: updatedAt
          ) {{
            nodes {{
              id
              identifier
              title
              url
              completedAt
              team {{ name }}
              project {{ name }}
            }}
          }}
        }}
        """
    )
    if data is None:
        return None
    nodes = ((data.get("issues") or {}).get("nodes")) or []
    entries = []
    for node in nodes:
        completed = node.get("completedAt") or ""
        team = (node.get("team") or {}).get("name")
        project = (node.get("project") or {}).get("name")
        context = " · ".join(part for part in [team, project] if part)
        entries.append(
            {
                "id": f"linear-{node.get('identifier')}",
                "date": completed[:10],
                "title": node.get("title"),
                "summary": f"Shipped {node.get('identifier')}" + (f" ({context})" if context else ""),
                "repos": [team] if team else [],
                "kind": "shipped",
                "url": node.get("url"),
                "source": "linear",
            }
        )
    return entries


def get_brain_changelog() -> list[dict[str, Any]]:
    """Changelog feed: recent Linear completions first, then curated seed entries."""
    live: list[dict[str, Any]] = []
    if os.getenv("LINEAR_API_KEY"):
        fetched = _linear_cached("shipped", _fetch_linear_shipped)
        if fetched:
            live = fetched
    return live + [
        {
            "id": "upstream-sync-2026-07",
            "date": "2026-07-30",
            "title": "Synced GPT Researcher to upstream (June 2026)",
            "summary": (
                "Merged upstream retrievers, MiniMax provider, and deep-research "
                "fixes while keeping Mastery Research HLT overlays intact."
            ),
            "repos": ["hlt-gpt-researcher"],
            "kind": "platform",
        },
        {
            "id": "mastery-brain-surfaces",
            "date": "2026-07-30",
            "title": "Mastery Brain tabs: Codebase, Vision, Changelog, Roadmap",
            "summary": (
                "Team-facing brain surfaces landed so marketing and ops can ask "
                "capability questions, store vision, and see what shipped."
            ),
            "repos": ["hlt-gpt-researcher"],
            "kind": "product",
        },
        {
            "id": "codegraph-estate",
            "date": "2026-07-30",
            "title": "Code-graph MCP for the five estate repos",
            "summary": (
                "GitNexus-backed structural search for mmm2, katailyst2, ebb, "
                "scrapervault, and nursing-mastery — preferred for Code scope."
            ),
            "repos": ["mmm2", "katailyst2", "ebb", "scrapervault", "nursing-mastery"],
            "kind": "infrastructure",
        },
    ]


def get_brain_roadmap() -> dict[str, Any]:
    """Roadmap payload: live Linear projects when LINEAR_API_KEY works, else seed."""
    linear_ready = bool(os.getenv("LINEAR_API_KEY") or os.getenv("LINEAR_MCP_URL"))
    if os.getenv("LINEAR_API_KEY"):
        live = _linear_cached("milestones", _fetch_linear_milestones)
        if live:
            return {
                "provider": "linear",
                "linear_configured": True,
                "milestones": live,
                "note": "Live Linear projects (nursingmastery workspace), cached 5 minutes.",
            }
    milestones = [
        {
            "id": "brain-v1",
            "title": "Mastery Brain v1 live for the team",
            "status": "in_progress",
            "summary": "Ask + Codebase + Vision + Changelog + Roadmap tabs; codegraph + Hermes sidecars.",
        },
        {
            "id": "deep-code-qa",
            "title": "Sub-2-minute ‘can we do X?’ answers",
            "status": "planned",
            "summary": "Codegraph + researcher path tuned for nontechnical teammates with visuals.",
        },
        {
            "id": "productboard",
            "title": "Productboard connector",
            "status": "planned",
            "summary": "Wire when API credentials exist; Linear is the primary roadmap source until then.",
        },
    ]
    return {
        "provider": "seed",
        "linear_configured": linear_ready,
        "milestones": milestones,
        "note": (
            "Connect LINEAR_API_KEY or LINEAR_MCP_URL for live Linear milestones. "
            "Productboard stays stubbed until credentials exist."
        ),
    }
