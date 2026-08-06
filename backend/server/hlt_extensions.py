"""HLT entrypoint and research-request router for the GPT Researcher app.

Isolated from upstream code so it can be re-applied after upstream merges
without touching `backend/server/app.py` beyond a single import line.

This module owns auth, integration readiness, MCP scope presets, and the
request router. Everything else lives in sibling `hlt_*` leaf modules that
this one composes — leaves never import back into here:

    hlt_text.py            shared stopwords + tokenizer
    hlt_media.py           Cloudinary search for the `media` scope
    hlt_brain.py           /api/brain/* estate context, library, Linear
    hlt_scope_inference.py picks scopes when the caller sends `auto`
    hlt_extensions.py      auth · readiness · presets · router · routes  <- here

The router is the single place a research request gets shaped. All three
doors go through `prepare_research_request`: the web UI (WebSocket), the MCP
tools, and `POST /report/`. `POST /api/quick_search` is the one deliberate
exception — it is web-only, because it runs no MCP tools and reads no local
corpora (see its docstring in `app.py`).

Adds:
  1. `GET /health` - dedicated liveness probe (Railway healthcheck target).
  2. `X-API-Key` middleware - rejects unauthenticated requests outside the
     frontend shell and static assets. Set `API_AUTH_KEY` in Railway env; if
     unset, the middleware is a no-op (useful for local dev).
  3. `/api/brain/*` and scope-aware research request preparation.

Usage (one line at the bottom of `app.py`):

    from server.hlt_extensions import install as install_hlt_extensions
    install_hlt_extensions(app)

Upstream merges: if `app.py` is regenerated, just re-add that one import.
"""
from __future__ import annotations

import hmac
import importlib.util
import json
import logging
import os
import re
import secrets
import time
from typing import Any, Iterable
import urllib.error
import urllib.request

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from gpt_researcher.utils.langfuse_observability import get_langfuse_runtime_status

from .hlt_brain import (
    _corpus_readiness,
    get_brain_audience,
    get_brain_changelog,
    get_brain_library,
    get_brain_repos,
    get_brain_roadmap,
    get_brain_vision_documents,
    create_linear_change_request,
    search_prior_reports,
)
from .hlt_media import _cloudinary_readiness, search_cloudinary_assets
from .hlt_scope_inference import infer_research_scope

# Re-exported above for callers and tests that treat this module as the HLT
# entrypoint; the implementations live in the sibling leaf modules.
__all__ = [
    "api_key_is_valid",
    "create_websocket_token",
    "get_brain_audience",
    "get_brain_changelog",
    "get_brain_library",
    "get_brain_repos",
    "get_brain_roadmap",
    "get_brain_vision_documents",
    "get_hlt_readiness",
    "install",
    "prepare_research_request",
    "resolve_research_scope",
    "search_cloudinary_assets",
    "search_prior_reports",
    "websocket_token_is_valid",
]

logger = logging.getLogger(__name__)
_WS_TOKEN_TTL_SECONDS = 120
_CODEGRAPH_HEALTH_TTL_SECONDS = 30
_codegraph_health_cache: tuple[float, list[dict[str, Any]]] | None = None

# Paths that must remain callable without auth so the frontend + Railway
# healthcheck + static assets keep working. Tight allowlist — every other
# route requires `X-API-Key`.
_PUBLIC_EXACT: set[str] = {"/", "/health", "/favicon.ico"}
_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/site/",      # Next.js static build
    "/static/",    # Any static mounts
)

# The canonical HLT estate repositories for codebase-scoped research. Override
# with a comma-separated HLT_CODEBASE_REPOS env when the estate map changes.
_DEFAULT_CODEBASE_REPOS = (
    "Awhitter/nursing-mastery (nurse-facing frontend — the career home)",
    "Awhitter/ScraperVault (nurse-recruiting backend — jobs, employers, people, applications)",
    "Awhitter/katailyst2 (AI primitives, registry, and creation engine)",
    "Awhitter/MMM2 (multimedia engine)",
    "Awhitter/evidence-based-business (EBB — metrics and analytics layer)",
)

# Katailyst2 is the current generation; www.katailyst.com is v1/legacy.
_DEFAULT_KATAILYST_MCP_URL = "https://katailyst2.vercel.app/mcp"

_SCOPE_INSTRUCTIONS = {
    "codebase": (
        "Use available codebase/repository context. Prefer implementation files, "
        "repo maps, pull requests, and architecture notes over generic web sources."
    ),
    "cms": (
        "Use available Katailyst2 registry, knowledge-base, playbook, skill, and "
        "ecosystem-map context when it is relevant to the question. Do not treat "
        "this as corporate CMS or question-bank access."
    ),
    "qbank": (
        "Use read-only corporate CMS and question-bank context through the "
        "Katailyst hlt-partner-api tool path when it is available. Never write "
        "to corporate CMS/QBank, and clearly say when that source was not inspected."
    ),
    "metrics": (
        "Use available metrics/analytics context, including Metabase-backed data, "
        "when it is relevant. Clearly separate measured data from inference."
    ),
    "firecrawl": (
        "For external pages, prefer high-quality extraction and crawling when the "
        "deployment has Firecrawl configured."
    ),
    "media": (
        "Use Cloudinary media-library context when it is relevant. Treat returned "
        "assets as read-only references for examples, visual direction, and reuse."
    ),
    "audience": (
        "Ground the research in audience truth: what nurses and nursing students "
        "actually say. Prioritize forums (Reddit r/nursing, r/StudentNurse, "
        "allnurses.com), most-upvoted threads, comment sections, and reviews. "
        "Quote the audience verbatim with links, capture recurring pain points and "
        "the exact language they use, and rank findings by engagement (upvotes, "
        "replies) rather than by what sources claim. Also consult the internal "
        "audience corpus (voice-of-nurse briefs and quote banks) when present."
    ),
    "recruiting": (
        "Specialize in nurse recruiting. Consult the internal Nursing Mastery "
        "content inventory (nursingmastery.com) and audience corpus when present, "
        "and treat www.nursingmastery.com as our own site. Compare our coverage "
        "against the best recruiting and career content on earth, in any industry, "
        "and say which of our pages a finding affects. The north star: help nurses "
        "get a better first job with less effort."
    ),
}

_DEPTH_INSTRUCTIONS = {
    "fast": "Keep the research narrow and fast. Prioritize the most relevant sources.",
    "balanced": "Balance speed and depth. Use enough context to answer confidently.",
    "deep": "Go deeper. Compare sources, inspect primary context, and surface tradeoffs.",
}

# Identity ground truth for scoped runs. Without it the planner free-associates
# on estate names — e.g. "nursing mastery" became an NCLEX-app comparison against
# ATI/Kaplan/UWorld instead of our recruiting platform.
_ESTATE_GLOSSARY = (
    "Company context (ground truth — do not re-derive it from the open web): "
    "HLT / Higher Learning Technologies (hltcorp.com) is a ~25-person healthcare "
    "education company whose current focus is nurse recruiting. 'Nursing Mastery' "
    "means www.nursingmastery.com — HLT's own nurse recruiting platform (job "
    "board, Nurse Pay Check pay-comparison tool, career content; code lives in "
    "the Awhitter/nursing-mastery repo, data in Awhitter/ScraperVault). HLT also "
    "ships Mastery-branded exam-prep apps (NCLEX etc.); only interpret a question "
    "that way when it is explicitly about test prep. Other estate names: "
    "Katailyst2 (AI registry/orchestration), MMM2 (media generation), EBB "
    "(metrics). For questions about these properties, treat our own sites, repos, "
    "and internal corpus as primary sources; third-party review chatter is "
    "secondary color, not ground truth."
)

_ESTATE_NAME_PATTERN = re.compile(
    r"nursing[\s_-]?mastery|katailyst|hltcorp|\bhlt\b|scraper[\s_-]?vault"
    r"|\bmmm2?\b|multimedia[\s_-]?mastery|evidence[\s_-]?based[\s_-]?business|\bebb\b",
    re.IGNORECASE,
)


def _mentions_estate(task: str) -> bool:
    return bool(_ESTATE_NAME_PATTERN.search(task or ""))


# "Study what's already working" doctrine as an executable research mode.
_RESEARCH_MODES = ("standard", "top1")
_TOP1_MODE_INSTRUCTIONS = (
    "Run this as a top-1% study. Steps, in order: "
    "(1) Find the best-performing examples of this thing anywhere on earth — "
    "usually outside our industry. Judge winners by feet-voting signals (what "
    "people buy, share, finish, upvote, return to), never by what anyone claims. "
    "(2) Distill WHY each winner actually works — the underlying mechanism "
    "(format, hook, promise, feedback loop, incentive design, distribution). "
    "Winners often misdiagnose their own success, so separate the stated reason "
    "from the real driver. "
    "(3) Propose how the mechanism rhymes with our niche (nursing / nurse "
    "recruiting) — an adapted version that feels native, not a clone. "
    "(4) Verify against customer truth: check what our audience actually asks, "
    "complains about, and upvotes (forums, reviews, search behavior, and the "
    "internal audience corpus when present). "
    "Structure the report around: Winners found (with receipts), Mechanisms "
    "distilled, Rhyme proposals for us, Audience verification."
)

_SCOPE_KEYS = (
    "codebase",
    "cms",
    "qbank",
    "metrics",
    "firecrawl",
    "media",
    "audience",
    "recruiting",
)
_MCP_PRESETS = ("katailyst", "codegraph", "github", "metabase", "apify", "qbank")
_DEFAULT_APIFY_MCP_URL = "https://mcp.apify.com"


def api_key_is_valid(provided: str | None) -> bool:
    """Return whether `provided` matches API_AUTH_KEY.

    When API_AUTH_KEY is unset, auth is intentionally disabled for local dev.
    """

    expected = os.getenv("API_AUTH_KEY") or None
    if expected is None:
        return True
    return bool(provided and hmac.compare_digest(provided, expected))


def create_websocket_token() -> str:
    """Create a short-lived token for browser WebSocket clients."""

    api_key = os.getenv("API_AUTH_KEY") or None
    if api_key is None:
        return "local-dev"

    issued_at = str(int(time.time()))
    nonce = secrets.token_urlsafe(18)
    payload = f"{issued_at}.{nonce}"
    signature = hmac.new(api_key.encode(), payload.encode(), "sha256").hexdigest()
    return f"{payload}.{signature}"


def websocket_token_is_valid(token: str | None) -> bool:
    """Validate a short-lived browser WebSocket token."""

    api_key = os.getenv("API_AUTH_KEY") or None
    if api_key is None:
        return True
    if not token:
        return False

    try:
        issued_at_text, nonce, provided_signature = token.split(".", 2)
        issued_at = int(issued_at_text)
    except (TypeError, ValueError):
        return False

    now = int(time.time())
    if issued_at > now + 30 or now - issued_at > _WS_TOKEN_TTL_SECONDS:
        return False

    payload = f"{issued_at_text}.{nonce}"
    expected_signature = hmac.new(api_key.encode(), payload.encode(), "sha256").hexdigest()
    return hmac.compare_digest(provided_signature, expected_signature)


def _bearer_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _katailyst_mcp_url() -> str:
    """Katailyst MCP endpoint, preferring the Katailyst2 generation."""

    return (
        os.getenv("KATAILYST2_MCP_URL")
        or os.getenv("KATAILYST_MCP_URL")
        or _DEFAULT_KATAILYST_MCP_URL
    )


def _katailyst_mcp_token() -> str | None:
    """Katailyst MCP bearer token, preferring the Katailyst2 (`kata_…`) token."""

    return (
        os.getenv("KATAILYST2_MCP_TOKEN")
        or os.getenv("KATAILYST_MCP_TOKEN")
        or os.getenv("KATAILYST_AUTH_TOKEN")
    )


def _apify_token() -> str | None:
    """Apify token for the hosted Apify MCP server (mcp.apify.com)."""

    return os.getenv("APIFY_TOKEN") or os.getenv("APIFY_API_TOKEN")


def _codebase_repos() -> tuple[str, ...]:
    raw = os.getenv("HLT_CODEBASE_REPOS")
    if raw:
        repos = tuple(part.strip() for part in raw.split(",") if part.strip())
        if repos:
            return repos
    return _DEFAULT_CODEBASE_REPOS


def _scope_instruction(key: str) -> str:
    """Scope instruction text; codebase names the canonical estate repos."""

    if key == "codebase":
        repos = "; ".join(_codebase_repos())
        return (
            "Answer as a careful internal code researcher for nontechnical teammates. "
            "Use available codebase/repository context (prefer codegraph MCP "
            "tools: list_repos, repo_overview, query, context, impact, trace, and "
            "verify_source_ref when available). "
            "The canonical HLT "
            f"repositories are: {repos}. Prefer these repositories' implementation "
            "files, repo maps, pull requests, and architecture notes over generic "
            "web sources, ignore legacy/archived repositories unless explicitly "
            "asked, and say which repository each finding came from. Treat "
            "ScraperVault as recruiting/profile/application authority, Nursing "
            "Mastery as the nurse-facing experience, Katailyst as capability "
            "authority, and EBB/PostHog as measurement evidence. Distinguish "
            "implemented now, documented intent, roadmap, and unknown. Every "
            "implementation claim must cite a retrieved existing file, route, "
            "symbol, schema, or configuration using an immutable GitHub URL with "
            "the exact 40-character commit SHA; validate the path before presenting "
            "it as verified. Never invent a file, endpoint, queue, database field, "
            "or integration. If Marketo or another live system was not inspected, "
            "say it is unavailable rather than guessing. Structure the answer as: "
            "Direct answer; What happens and when; What data is captured and where "
            "it is stored; Where the behavior lives; How to change it; Sources, "
            "freshness, and anything that could not be verified."
        )
    return _SCOPE_INSTRUCTIONS[key]


def _append_unique_mcp_config(configs: list[dict[str, Any]], config: dict[str, Any]) -> None:
    name = config.get("name")
    if name and any(existing.get("name") == name for existing in configs):
        return
    configs.append(config)


def _firecrawl_import_available() -> bool:
    return importlib.util.find_spec("firecrawl") is not None


def _preset_readiness(preset: str) -> dict[str, Any]:
    if preset == "katailyst":
        token = _katailyst_mcp_token()
        return {
            "status": "ready" if token else "unavailable",
            "configured": bool(token),
            "missing": [] if token else ["KATAILYST2_MCP_TOKEN"],
            "url_configured": bool(
                os.getenv("KATAILYST2_MCP_URL") or os.getenv("KATAILYST_MCP_URL")
            ),
        }
    if preset == "codegraph":
        url = os.getenv("CODEGRAPH_MCP_URL")
        return {
            "status": "ready" if url else "unavailable",
            "configured": bool(url),
            "missing": [] if url else ["CODEGRAPH_MCP_URL"],
            "token_configured": bool(os.getenv("CODEGRAPH_MCP_TOKEN")),
        }
    if preset == "github":
        url = os.getenv("GITHUB_MCP_URL")
        return {
            "status": "ready" if url else "unavailable",
            "configured": bool(url),
            "missing": [] if url else ["GITHUB_MCP_URL"],
            "token_configured": bool(os.getenv("GITHUB_MCP_TOKEN")),
        }
    if preset == "apify":
        token = _apify_token()
        return {
            "status": "ready" if token else "unavailable",
            "configured": bool(token),
            "missing": [] if token else ["APIFY_TOKEN"],
            "url_configured": bool(os.getenv("APIFY_MCP_URL")),
        }
    if preset == "metabase":
        url = os.getenv("METABASE_MCP_URL")
        fallback_token = _katailyst_mcp_token()
        fallback_ready = bool(fallback_token)
        direct_ready = bool(url)
        return {
            "status": "ready" if direct_ready or fallback_ready else "unavailable",
            "configured": direct_ready or fallback_ready,
            "missing": [] if direct_ready or fallback_ready else ["METABASE_MCP_URL", "KATAILYST2_MCP_TOKEN"],
            "token_configured": bool(os.getenv("METABASE_MCP_TOKEN")),
            "provider": "metabase" if direct_ready else "katailyst_metrics_fallback",
        }
    if preset == "qbank":
        # Dedicated partner-API MCP for the 70k-item question bank. Until the
        # credentials exist, qbank scope rides the Katailyst tool path.
        url = os.getenv("QBANK_MCP_URL")
        return {
            "status": "ready" if url else "unavailable",
            "configured": bool(url),
            "missing": [] if url else ["QBANK_MCP_URL"],
            "token_configured": bool(os.getenv("QBANK_MCP_TOKEN")),
        }
    return {
        "status": "unknown",
        "configured": False,
        "missing": [],
    }


def _codegraph_repository_readiness() -> list[dict[str, Any]]:
    """Read per-repository index truth from the codegraph health endpoint."""
    global _codegraph_health_cache
    now = time.time()
    if _codegraph_health_cache and now - _codegraph_health_cache[0] < _CODEGRAPH_HEALTH_TTL_SECONDS:
        return _codegraph_health_cache[1]
    configured = os.getenv("CODEGRAPH_MCP_URL")
    if not configured:
        return []
    # /readiness, not /health: the sidecar's public health endpoint is liveness
    # only, because it is world-readable and index truth names private repos
    # and their commit SHAs. This route needs the bearer token.
    readiness_url = re.sub(r"/mcp/?$", "/readiness", configured)
    token = os.getenv("CODEGRAPH_MCP_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(readiness_url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - configured service URL
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        # Say so. Swallowing this renders as an empty repo panel in the UI,
        # which reads as "the estate has no repos" rather than "the probe failed".
        logger.warning("Code-graph readiness probe failed (%s): %s", readiness_url, exc)
        return []
    repositories = body.get("repositories") if isinstance(body, dict) else None
    result = repositories if isinstance(repositories, list) else []
    _codegraph_health_cache = (now, result)
    return result


def _firecrawl_readiness() -> dict[str, Any]:
    has_key = bool(os.getenv("FIRECRAWL_API_KEY"))
    has_package = _firecrawl_import_available()
    missing = []
    if not has_key:
        missing.append("FIRECRAWL_API_KEY")
    if not has_package:
        missing.append("firecrawl-py")
    return {
        "status": "ready" if has_key and has_package else "unavailable",
        "configured": has_key and has_package,
        "missing": missing,
        "server_url_configured": bool(os.getenv("FIRECRAWL_SERVER_URL")),
        "scraper": "firecrawl" if has_key and has_package else "default",
    }




def _status_from_components(statuses: list[str]) -> str:
    if statuses and all(status == "ready" for status in statuses):
        return "ready"
    if any(status == "ready" for status in statuses):
        return "partial"
    return "unavailable"




def get_hlt_readiness() -> dict[str, Any]:
    """Return browser-safe readiness for HLT scope-backed integrations."""

    preset_statuses = {preset: _preset_readiness(preset) for preset in _MCP_PRESETS}
    firecrawl_status = _firecrawl_readiness()
    cloudinary_status = _cloudinary_readiness()
    audience_corpus = _corpus_readiness("audience")
    recruiting_corpus = _corpus_readiness("recruiting")
    repository_readiness = _codegraph_repository_readiness()
    repository_statuses = [
        str(repo.get("status") or "unavailable")
        for repo in repository_readiness
        if isinstance(repo, dict)
    ]
    if len(repository_statuses) == len(_codebase_repos()) and all(
        status == "ready" for status in repository_statuses
    ):
        codegraph_repository_status = "ready"
    elif any(status in {"ready", "partial"} for status in repository_statuses):
        codegraph_repository_status = "partial"
    else:
        codegraph_repository_status = "unavailable"
    code_backend_status = (
        "ready"
        if codegraph_repository_status == "ready"
        or (
            preset_statuses["codegraph"]["configured"] is False
            and preset_statuses["github"]["status"] == "ready"
        )
        else "partial"
        if codegraph_repository_status == "partial"
        or preset_statuses["github"]["status"] == "ready"
        else "unavailable"
    )

    integrations = {
        "codebase": {
            # Ready when Katailyst is up and at least one code backend
            # (preferred codegraph, else GitHub) is configured.
            "status": (
                "ready"
                if preset_statuses["katailyst"]["status"] == "ready"
                and code_backend_status == "ready"
                else _status_from_components([
                    preset_statuses["katailyst"]["status"],
                    code_backend_status,
                ])
            ),
            "components": {
                "katailyst": preset_statuses["katailyst"]["status"],
                "codegraph": codegraph_repository_status,
                "github": preset_statuses["github"]["status"],
            },
            "repositories": repository_readiness,
            "missing": sorted(set(
                preset_statuses["katailyst"]["missing"]
                + (
                    []
                    if code_backend_status in {"ready", "partial"}
                    else preset_statuses["codegraph"]["missing"]
                    + preset_statuses["github"]["missing"]
                )
                + [
                    f"{repo.get('repo')}: {repo.get('error') or repo.get('status')}"
                    for repo in repository_readiness
                    if isinstance(repo, dict) and repo.get("status") != "ready"
                ]
            )),
        },
        "cms": {
            "status": preset_statuses["katailyst"]["status"],
            "components": {"katailyst": preset_statuses["katailyst"]["status"]},
            "missing": preset_statuses["katailyst"]["missing"],
        },
        "qbank": {
            # Prefer the dedicated partner-API MCP; Katailyst tool path remains
            # the fallback until QBANK_MCP_URL exists.
            "status": (
                "ready"
                if preset_statuses["qbank"]["status"] == "ready"
                or preset_statuses["katailyst"]["status"] == "ready"
                else "unavailable"
            ),
            "components": {
                "qbank_partner_api": preset_statuses["qbank"]["status"],
                "katailyst": preset_statuses["katailyst"]["status"],
            },
            "missing": (
                []
                if preset_statuses["qbank"]["status"] == "ready"
                or preset_statuses["katailyst"]["status"] == "ready"
                else sorted(set(
                    preset_statuses["qbank"]["missing"]
                    + preset_statuses["katailyst"]["missing"]
                ))
            ),
            "access": "read_only_checked_on_use",
        },
        "metrics": {
            "status": preset_statuses["metabase"]["status"],
            "components": {
                "metabase": "ready" if os.getenv("METABASE_MCP_URL") else "unavailable",
                "katailyst_metrics_fallback": (
                    "ready"
                    if preset_statuses["metabase"].get("provider") == "katailyst_metrics_fallback"
                    else "inactive"
                ),
            },
            "missing": preset_statuses["metabase"]["missing"],
            "provider": preset_statuses["metabase"].get("provider"),
        },
        "firecrawl": {
            "status": firecrawl_status["status"],
            "components": {
                "firecrawl": firecrawl_status["status"],
                "apify": preset_statuses["apify"]["status"],
            },
            "missing": firecrawl_status["missing"],
            "scraper": firecrawl_status["scraper"],
        },
        "media": {
            "status": cloudinary_status["status"],
            "components": {"cloudinary": cloudinary_status["status"]},
            "missing": cloudinary_status["missing"],
            "access": "read_only_server_side",
        },
        "audience": {
            # Ready when the voice-of-nurse corpus exists; live sweeps degrade
            # to partial when only the scrapers are configured.
            "status": (
                "ready"
                if audience_corpus["status"] == "ready"
                else (
                    "partial"
                    if firecrawl_status["status"] == "ready"
                    or preset_statuses["apify"]["status"] == "ready"
                    else "unavailable"
                )
            ),
            "components": {
                "corpus": audience_corpus["status"],
                "firecrawl": firecrawl_status["status"],
                "apify": preset_statuses["apify"]["status"],
            },
            "missing": audience_corpus["missing"] if audience_corpus["status"] != "ready" else [],
            "document_count": audience_corpus["document_count"],
        },
        "recruiting": {
            "status": (
                "ready"
                if recruiting_corpus["status"] == "ready"
                else (
                    "partial"
                    if firecrawl_status["status"] == "ready"
                    else "unavailable"
                )
            ),
            "components": {
                "content_inventory": recruiting_corpus["status"],
                "audience_corpus": audience_corpus["status"],
                "firecrawl": firecrawl_status["status"],
            },
            "missing": recruiting_corpus["missing"] if recruiting_corpus["status"] != "ready" else [],
            "document_count": recruiting_corpus["document_count"],
        },
    }

    status_values = [entry["status"] for entry in integrations.values()]
    ready_count = sum(1 for status in status_values if status == "ready")
    partial_count = sum(1 for status in status_values if status == "partial")
    unavailable_count = sum(1 for status in status_values if status == "unavailable")

    aggregate = "ready"
    if partial_count:
        aggregate = "partial"
    if unavailable_count and ready_count == 0 and partial_count == 0:
        aggregate = "needs_config"
    elif unavailable_count:
        aggregate = "partial"

    return {
        "status": aggregate,
        "integrations": integrations,
        "preset_statuses": preset_statuses,
        "scraper": firecrawl_status,
        "summary": {
            "ready": ready_count,
            "partial": partial_count,
            "unavailable": unavailable_count,
        },
    }


def _mcp_config_for_preset(preset: str, name: str | None = None) -> dict[str, Any] | None:
    name = name or preset
    if preset == "katailyst":
        readiness = _preset_readiness("katailyst")
        if readiness["status"] != "ready":
            logger.warning("Skipping Katailyst MCP preset: KATAILYST2_MCP_TOKEN is unset")
            return None
        url = _katailyst_mcp_url()
        token = _katailyst_mcp_token()
    elif preset == "codegraph":
        readiness = _preset_readiness("codegraph")
        if readiness["status"] != "ready":
            logger.warning("Skipping code-graph MCP preset: CODEGRAPH_MCP_URL is unset")
            return None
        url = os.getenv("CODEGRAPH_MCP_URL")
        token = os.getenv("CODEGRAPH_MCP_TOKEN")
    elif preset == "github":
        readiness = _preset_readiness("github")
        if readiness["status"] != "ready":
            logger.warning("Skipping GitHub MCP preset: GITHUB_MCP_URL is unset")
            return None
        url = os.getenv("GITHUB_MCP_URL")
        token = os.getenv("GITHUB_MCP_TOKEN")
    elif preset == "apify":
        readiness = _preset_readiness("apify")
        if readiness["status"] != "ready":
            logger.warning("Skipping Apify MCP preset: APIFY_TOKEN is unset")
            return None
        url = os.getenv("APIFY_MCP_URL") or _DEFAULT_APIFY_MCP_URL
        token = _apify_token()
    elif preset == "metabase":
        readiness = _preset_readiness("metabase")
        if readiness["status"] != "ready":
            logger.warning("Skipping metrics MCP preset: METABASE_MCP_URL and Katailyst fallback are unavailable")
            return None
        if readiness.get("provider") == "katailyst_metrics_fallback":
            url = _katailyst_mcp_url()
            token = _katailyst_mcp_token()
        else:
            url = os.getenv("METABASE_MCP_URL")
            token = os.getenv("METABASE_MCP_TOKEN")
    elif preset == "qbank":
        readiness = _preset_readiness("qbank")
        if readiness["status"] != "ready":
            logger.warning("Skipping QBank MCP preset: QBANK_MCP_URL is unset")
            return None
        url = os.getenv("QBANK_MCP_URL")
        token = os.getenv("QBANK_MCP_TOKEN")
    else:
        logger.warning("Skipping unknown MCP preset: %s", preset)
        return None

    return {
        "name": name,
        "connection_url": url,
        "connection_type": "streamable_http",
        "connection_headers": _bearer_headers(token),
    }


def expand_mcp_presets(mcp_configs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Expand HLT server-side MCP presets without exposing tokens to browsers."""

    expanded: list[dict[str, Any]] = []
    for config in mcp_configs or []:
        preset = config.get("preset")
        if not preset:
            expanded.append(config)
            continue

        name = config.get("name") or preset
        expanded_config = _mcp_config_for_preset(preset, name)
        if expanded_config:
            expanded.append(expanded_config)
    return expanded


def _sanitize_client_mcp_configs(
    mcp_configs: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop browser-supplied MCP configs that need a local process.

    The upstream Preferences panel offers sample configs like
    `{"command": "npx", ...}`. This container has no Node runtime, and one
    failing stdio server poisons tool loading for every MCP server in the
    run (`Error getting MCP tools: No such file or directory: 'npx'`), so
    only hosted (http/ws) configs and server-side preset references pass.
    """
    safe: list[dict[str, Any]] = []
    dropped: list[str] = []
    for config in mcp_configs or []:
        if not isinstance(config, dict):
            continue
        url = str(config.get("connection_url") or "")
        hosted = url.startswith(("http://", "https://", "ws://", "wss://"))
        if config.get("preset") or (hosted and not config.get("command")):
            safe.append(config)
        else:
            dropped.append(str(config.get("name") or "unnamed"))
    if dropped:
        logger.warning(
            "Dropped browser MCP configs that require a local process: %s",
            ", ".join(dropped),
        )
    return safe, dropped


def _scope_status(
    key: str,
    *,
    requested: bool,
    readiness: dict[str, Any],
) -> dict[str, Any]:
    integration = readiness["integrations"][key]
    status = integration["status"] if requested else "inactive"
    active = requested and status in {"ready", "partial"}
    degraded = requested and status in {"partial", "unavailable"}
    return {
        "requested": requested,
        "status": status,
        "active": active,
        "degraded": degraded,
        "components": integration.get("components", {}),
        "missing": integration.get("missing", []),
        "scraper": integration.get("scraper"),
    }


def resolve_research_scope(
    *,
    mcp_configs: list[dict[str, Any]] | None,
    research_scope: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
    """Resolve browser scope metadata into server-side configs and status."""

    scope = research_scope or {}
    readiness = get_hlt_readiness()
    configs, dropped_client_configs = _sanitize_client_mcp_configs(mcp_configs)

    requested = {key: bool(scope.get(key)) for key in _SCOPE_KEYS}
    if requested["codebase"] or requested["cms"] or requested["qbank"]:
        _append_unique_mcp_config(configs, {"name": "katailyst", "preset": "katailyst"})
    if requested["codebase"]:
        # Prefer structural code-graph MCP; keep GitHub MCP as fallback.
        codegraph_ready = readiness["preset_statuses"]["codegraph"]["status"] == "ready"
        if codegraph_ready:
            _append_unique_mcp_config(configs, {"name": "codegraph", "preset": "codegraph"})
        else:
            _append_unique_mcp_config(configs, {"name": "github", "preset": "github"})
    if requested["metrics"]:
        _append_unique_mcp_config(configs, {"name": "metabase", "preset": "metabase"})
    if requested["qbank"]:
        # Dedicated partner-API MCP when configured (Katailyst already added above).
        if readiness["preset_statuses"]["qbank"]["status"] == "ready":
            _append_unique_mcp_config(configs, {"name": "qbank", "preset": "qbank"})
    if requested["firecrawl"] or requested["audience"]:
        # Deep-web and audience scopes: add Apify's hosted MCP (actor
        # marketplace) when a token exists so research can reach forums and
        # social sources Firecrawl cannot.
        if readiness["preset_statuses"]["apify"]["status"] == "ready":
            _append_unique_mcp_config(configs, {"name": "apify", "preset": "apify"})

    expanded_configs = expand_mcp_presets(configs)
    scope_statuses = {
        key: _scope_status(key, requested=requested[key], readiness=readiness)
        for key in _SCOPE_KEYS
    }
    # Audience and recruiting sweeps lean on Firecrawl scraping as well.
    firecrawl_ready = readiness["scraper"]["status"] == "ready"
    scraper_override = (
        "firecrawl"
        if scope_statuses["firecrawl"]["active"]
        or (firecrawl_ready and (scope_statuses["audience"]["active"] or scope_statuses["recruiting"]["active"]))
        else None
    )

    active_sources = [
        key for key, status in scope_statuses.items()
        if status["active"]
    ]
    degraded_sources = [
        key for key, status in scope_statuses.items()
        if status["degraded"]
    ]

    metadata = {
        "enabled_sources": [key for key in _SCOPE_KEYS if requested[key]],
        "active_sources": active_sources,
        "degraded_sources": degraded_sources,
        "scope_statuses": scope_statuses,
        "preset_statuses": readiness["preset_statuses"],
        "depth": scope.get("depth") if scope.get("depth") in _DEPTH_INSTRUCTIONS else "balanced",
        "mcp_server_count": len(expanded_configs),
        "dropped_mcp_configs": dropped_client_configs,
        "scraper": {
            "requested": requested["firecrawl"],
            "active": bool(scraper_override),
            "selected": scraper_override or "default",
        },
        "media": {
            "requested": requested["media"],
            "searched": False,
            "asset_count": 0,
        },
    }
    return expanded_configs, scraper_override, metadata


def prepare_research_request(
    *,
    task: str,
    mcp_enabled: bool,
    mcp_strategy: str,
    mcp_configs: list[dict[str, Any]] | None,
    research_scope: dict[str, Any] | None,
) -> tuple[str, bool, str, list[dict[str, Any]], dict[str, Any], str | None]:
    """Apply HLT research-scope metadata to a GPT Researcher request.

    This keeps the browser payload token-free. The frontend sends booleans such
    as `codebase` or `cms`; this server-side helper expands them into safe MCP
    presets and adds concise research instructions to the task.

    Auto scope: when the caller pins no scope (research_scope is None, or it
    carries `auto: true` with no explicit scope keys), circumstantial
    inference decides which internal scopes the task needs. Explicit scope
    selections always win, and inference never activates an unready
    integration.
    """

    scope = dict(research_scope or {})
    explicit_keys = [key for key in _SCOPE_KEYS if bool(scope.get(key))]
    auto_requested = (research_scope is None or bool(scope.get("auto"))) and not explicit_keys
    auto_scope_metadata: dict[str, Any] = {
        "requested": auto_requested,
        "applied": [],
        "reasons": {},
        "llm_used": False,
        "skipped_unready": [],
    }
    if auto_requested:
        readiness_statuses = {
            key: value["status"]
            for key, value in get_hlt_readiness()["integrations"].items()
        }
        inference = infer_research_scope(task, readiness=readiness_statuses)
        for key in inference["scopes"]:
            scope[key] = True
        auto_scope_metadata.update(
            applied=inference["scopes"],
            reasons=inference["reasons"],
            llm_used=inference["llm_used"],
            skipped_unready=inference["skipped_unready"],
        )
    research_scope = scope

    enabled_keys = [key for key in _SCOPE_KEYS if bool(scope.get(key))]
    depth = scope.get("depth") if scope.get("depth") in _DEPTH_INSTRUCTIONS else "balanced"
    mode = scope.get("mode") if scope.get("mode") in _RESEARCH_MODES else "standard"
    memory_enabled = scope.get("memory", True) is not False

    expanded_configs, scraper_override, hlt_scope_metadata = resolve_research_scope(
        mcp_configs=mcp_configs,
        research_scope=research_scope,
    )
    hlt_scope_metadata["mode"] = mode
    hlt_scope_metadata["auto_scope"] = auto_scope_metadata

    prior_reports = search_prior_reports(task, limit=3) if memory_enabled else []
    hlt_scope_metadata["prior_research"] = [
        {"id": item["id"], "question": item["question"], "date": item["date"]}
        for item in prior_reports
    ]
    if bool(scope.get("media")):
        media_result = search_cloudinary_assets(task)
        assets = media_result.get("assets", [])
        hlt_scope_metadata["media"] = {
            "requested": True,
            "searched": media_result.get("status") in {"ready", "empty"},
            "status": media_result.get("status"),
            "asset_count": len(assets) if isinstance(assets, list) else 0,
            "assets": assets if isinstance(assets, list) else [],
            "warnings": media_result.get("warnings", []),
        }
        if media_result.get("status") in {"degraded", "unavailable"}:
            if "media" not in hlt_scope_metadata["degraded_sources"]:
                hlt_scope_metadata["degraded_sources"].append("media")
    next_mcp_enabled = bool(mcp_enabled or expanded_configs)
    next_mcp_strategy = "fast" if depth == "fast" else "deep"

    if (
        not enabled_keys
        and depth == "balanced"
        and mode == "standard"
        and not prior_reports
        and not _mentions_estate(task)
    ):
        return task, next_mcp_enabled, mcp_strategy, expanded_configs, hlt_scope_metadata, scraper_override

    instruction_lines = [_ESTATE_GLOSSARY, _DEPTH_INSTRUCTIONS[depth]]
    if mode == "top1":
        instruction_lines.append(_TOP1_MODE_INSTRUCTIONS)
    instruction_lines.extend(
        _scope_instruction(key)
        for key in hlt_scope_metadata["active_sources"]
        if key in _SCOPE_INSTRUCTIONS
    )
    if prior_reports:
        prior_lines = "; ".join(
            f"\"{item['question']}\" ({item['date'] or 'undated'}, id {item['id']})"
            for item in prior_reports
        )
        instruction_lines.append(
            "Prior internal research exists on related questions — build on it "
            f"instead of restarting, and note what changed since: {prior_lines}."
        )
    for key in hlt_scope_metadata["degraded_sources"]:
        if key in enabled_keys:
            instruction_lines.append(
                f"{key} scope was requested but is only partially available or unconfigured; "
                "do not imply unavailable internal data was inspected."
            )
    media = hlt_scope_metadata.get("media", {})
    media_assets = media.get("assets") if isinstance(media, dict) else []
    if isinstance(media_assets, list) and media_assets:
        instruction_lines.append(
            "Cloudinary media assets found below are read-only references. Cite public_id or URL when useful."
        )
    scoped_task = (
        f"{task}\n\n"
        "HLT research scope instructions:\n"
        + "\n".join(f"- {line}" for line in instruction_lines)
    )
    if isinstance(media_assets, list) and media_assets:
        scoped_task += "\n\nCloudinary media library context:\n" + "\n".join(
            "- "
            + "; ".join(
                part
                for part in [
                    f"public_id={asset.get('public_id')}",
                    f"type={asset.get('resource_type')}",
                    f"folder={asset.get('asset_folder')}" if asset.get("asset_folder") else "",
                    f"tags={', '.join(asset.get('tags') or [])}" if asset.get("tags") else "",
                    f"url={asset.get('secure_url')}" if asset.get("secure_url") else "",
                ]
                if part
            )
            for asset in media_assets
            if isinstance(asset, dict)
        )

    return scoped_task, next_mcp_enabled, next_mcp_strategy, expanded_configs, hlt_scope_metadata, scraper_override


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests missing a valid `X-API-Key` header.

    No-op when `API_AUTH_KEY` env is unset (local dev / upstream default).
    """

    def __init__(self, app, api_key: str | None, public_exact: set[str], public_prefixes: tuple[str, ...]):
        super().__init__(app)
        self._api_key = api_key
        self._public_exact = public_exact
        self._public_prefixes = public_prefixes

    async def dispatch(self, request: Request, call_next):
        if self._api_key is None:
            return await call_next(request)

        path = request.url.path
        if path in self._public_exact or path.startswith(self._public_prefixes):
            return await call_next(request)

        # CORS preflight must pass; CORSMiddleware handles the actual check.
        if request.method == "OPTIONS":
            return await call_next(request)

        provided = request.headers.get("x-api-key", "")
        if not provided:
            auth_header = request.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                provided = auth_header[7:].strip()
        if not provided or not hmac.compare_digest(provided, self._api_key):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid X-API-Key header"},
            )
        return await call_next(request)


def install(
    app: FastAPI,
    *,
    extra_public_exact: Iterable[str] | None = None,
    extra_public_prefixes: Iterable[str] | None = None,
) -> None:
    """Attach HLT extensions to the given FastAPI app."""

    public_exact = set(_PUBLIC_EXACT)
    if extra_public_exact:
        public_exact.update(extra_public_exact)

    public_prefixes = list(_PUBLIC_PREFIXES)
    if extra_public_prefixes:
        public_prefixes.extend(extra_public_prefixes)

    # 1. /health — used by Railway healthcheckPath (see railway.toml).
    @app.get("/health", tags=["hlt"])
    def health():  # noqa: D401
        readiness = get_hlt_readiness()
        return {
            "status": "ok",
            "service": "gpt-researcher-api",
            "version": os.getenv("RAILWAY_GIT_COMMIT_SHA", "local")[:7],
            "deploy_marker": os.getenv("HLT_DEPLOY_MARKER", "local"),
            "observability": {
                "langfuse": get_langfuse_runtime_status(),
            },
            "integrations": {
                "status": readiness["status"],
                "summary": readiness["summary"],
            },
        }

    @app.get("/api/hlt/readiness", tags=["hlt"])
    def readiness():  # noqa: D401
        return get_hlt_readiness()

    @app.post("/gather", tags=["hlt"])
    async def katailyst_gather(request: Request):  # noqa: D401
        """Katailyst2 HTTP gather adapter — maps quick_search results to typed findings."""
        from gpt_researcher import GPTResearcher

        body = await request.json()
        query_obj = body.get("query") or {}
        keyword = (
            query_obj.get("keyword")
            or query_obj.get("query")
            or query_obj.get("url")
            or ""
        )
        if not keyword:
            return JSONResponse(status_code=400, content={"detail": "query.keyword required"})

        max_findings = min(int(body.get("max_findings") or 10), 25)
        research_kind = body.get("research_kind") or "topic_deep_dive"

        kind_to_finding = {
            "seo_research": "content_gap",
            "competitor_research": "content_gap",
            "trend_scan": "trend",
            "forum_scan": "pain",
            "audience_language_mining": "language",
            "topic_deep_dive": "topic_opportunity",
            "cross_industry_scan": "trend",
        }
        default_finding = kind_to_finding.get(research_kind, "topic_opportunity")

        researcher = GPTResearcher(query=str(keyword), report_type="research_report")
        results = await researcher.quick_search(
            query=str(keyword),
            query_domains=query_obj.get("domains"),
            aggregated_summary=True,
        )

        findings = []
        if isinstance(results, str) and results.strip():
            findings.append(
                {
                    "finding_type": default_finding,
                    "summary": results.strip()[:240],
                    "detail_md": results.strip()[:4000],
                    "source_kind": "gpt_researcher",
                    "confidence": 0.72,
                }
            )
        elif isinstance(results, list):
            for item in results[:max_findings]:
                if isinstance(item, dict):
                    summary = str(item.get("title") or item.get("content") or item.get("snippet") or keyword)[:240]
                    url = item.get("url") or item.get("link")
                    findings.append(
                        {
                            "finding_type": default_finding,
                            "summary": summary,
                            "detail_md": str(item.get("content") or item.get("snippet") or summary)[:4000],
                            "source_url": url,
                            "source_kind": "gpt_researcher",
                            "confidence": 0.7,
                        }
                    )
                elif isinstance(item, str) and item.strip():
                    findings.append(
                        {
                            "finding_type": default_finding,
                            "summary": item.strip()[:240],
                            "detail_md": item.strip()[:4000],
                            "source_kind": "gpt_researcher",
                            "confidence": 0.65,
                        }
                    )

        return {"findings": findings[:max_findings], "cost_usd": 0.01, "external_scan_id": None}

    @app.get("/api/brain/repos", tags=["hlt", "brain"])
    def brain_repos():  # noqa: D401
        """Team-facing estate repo concept cards for the Codebase tab."""
        return {
            "repos": get_brain_repos(repository_readiness=_codegraph_repository_readiness())
        }

    @app.get("/api/brain/vision", tags=["hlt", "brain"])
    def brain_vision():  # noqa: D401
        """Markdown vision docs from DOC_PATH/vision (hybrid research corpus)."""
        return {"documents": get_brain_vision_documents()}

    @app.get("/api/brain/changelog", tags=["hlt", "brain"])
    def brain_changelog():  # noqa: D401
        """Interactive changelog feed (static seed + optional Linear later)."""
        return {"entries": get_brain_changelog()}

    @app.get("/api/brain/roadmap", tags=["hlt", "brain"])
    def brain_roadmap():  # noqa: D401
        """Roadmap milestones (Linear when configured; otherwise seed)."""
        return get_brain_roadmap()

    @app.get("/api/brain/audience", tags=["hlt", "brain"])
    def brain_audience():  # noqa: D401
        """Voice-of-nurse corpus + nursingmastery.com content inventory."""
        return get_brain_audience()

    @app.get("/api/brain/library", tags=["hlt", "brain"])
    def brain_library(q: str | None = None):  # noqa: D401
        """Searchable archive of past research runs (the memory layer)."""
        return get_brain_library(query=q)

    @app.post("/api/brain/change-request", tags=["hlt", "brain"])
    async def brain_change_request(request: Request):
        """Create a Linear issue only after an explicit confirmation."""
        try:
            payload = await request.json()
            return create_linear_change_request(payload if isinstance(payload, dict) else {})
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)
        except RuntimeError as error:
            return JSONResponse({"detail": str(error)}, status_code=503)

    # 2. API-key auth (opt-in via env).
    api_key = os.getenv("API_AUTH_KEY") or None
    app.add_middleware(
        APIKeyMiddleware,
        api_key=api_key,
        public_exact=public_exact,
        public_prefixes=tuple(public_prefixes),
    )

    if api_key:
        logger.info("HLT extensions: /health + X-API-Key middleware installed (auth enabled)")
    else:
        logger.info("HLT extensions: /health installed; auth disabled (API_AUTH_KEY unset)")
