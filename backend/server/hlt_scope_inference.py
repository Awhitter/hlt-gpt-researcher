"""Circumstantial scope inference for HLT research requests.

Decides which internal scopes (estate code repos, Katailyst2 registry,
metrics, media, audience, recruiting) a research query actually needs, so
the researcher pulls estate context when it is relevant and stays a plain
web researcher when it is not.

Design: cheap keyword/entity heuristics run first and are decisive for the
obvious cases in both directions — clear estate signals turn a scope on,
zero signals means pure web research with zero added latency or cost. Only
ambiguous weak-signal queries pay for a single fast-model LLM tiebreak,
bounded by a short timeout and falling back to heuristics-only on any
failure.

qbank (corporate CMS / question bank, read-only and sensitive) and
firecrawl (a scraping cost/quality lever, not a relevance signal) are never
auto-triggered; they stay explicit-only.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# Scopes inference may activate on its own, in stable presentation order.
INFERABLE_SCOPES = ("codebase", "cms", "metrics", "media", "audience", "recruiting")

_STRONG = 2
_WEAK = 1

_LLM_TIMEOUT_SECONDS = 6


def _compile(signals: tuple[tuple[int, str, str], ...]) -> tuple[tuple[int, re.Pattern[str], str], ...]:
    return tuple((weight, re.compile(pattern, re.IGNORECASE), reason) for weight, pattern, reason in signals)


# (weight, pattern, human-readable reason). A scope is selected outright at
# score >= _STRONG; a lone _WEAK hit makes it an LLM-tiebreak candidate.
_SIGNALS: dict[str, tuple[tuple[int, re.Pattern[str], str], ...]] = {
    "codebase": _compile((
        (_STRONG, r"nursing-mastery", "mentions the nursing-mastery repo"),
        (_STRONG, r"scraper\s?vault", "mentions ScraperVault"),
        (_STRONG, r"\bmmm2\b", "mentions MMM2"),
        (_STRONG, r"\bebb\b|evidence-based-business", "mentions EBB"),
        (_STRONG, r"\b(repos?|repository|repositories|codebase)\b", "asks about repositories"),
        (_STRONG, r"\bour (code|codebase|app|site|stack|frontend|backend)\b", "asks about our code"),
        (_STRONG, r"\bpull request(s)?\b", "mentions pull requests"),
        (_WEAK, r"\bkatailyst2?\b", "mentions Katailyst"),
        (_WEAK, r"\bnursing mastery\b", "mentions Nursing Mastery"),
        (_WEAK, r"\b(endpoints?|api route|schema|deployed?|architecture|implemented|implementation)\b", "uses implementation words"),
        (_WEAK, r"\b(apply|application) flow\b", "mentions the apply flow"),
        (_STRONG, r"\b(what|which) (attributes?|fields?|data) (do|does) (we|the app) (capture|collect|store).{0,30}\bnurs", "asks what nurse data the product captures"),
        (_STRONG, r"\bwhen (do|does) (we|the app) (capture|collect|ask for).{0,30}\bemail", "asks when email is captured"),
        (_STRONG, r"\b(do|does) (we|the app).{0,30}\bemail(s)?\b.{0,30}\bmarketo\b", "asks about the Marketo email handoff"),
        (_STRONG, r"\b(how (do|does)|what powers).{0,25}\b(job )?search\b", "asks how product search works"),
        (_STRONG, r"\b(onboarding|sign[- ]?up).{0,35}\b(questions?|fields?|change|edit)", "asks about onboarding implementation"),
        (_STRONG, r"\bhow (do|can|would) (i|we).{0,35}\b(change|edit|update)\b", "asks how to change implemented behavior"),
        (_STRONG, r"\bwhere (do|does|is|are).{0,40}\b(stored?|saved?|live|implemented)\b", "asks where product behavior or data lives"),
    )),
    "cms": _compile((
        (_STRONG, r"\bkatailyst2?\b", "mentions Katailyst"),
        (_STRONG, r"\bregistry\b", "mentions the registry"),
        (_STRONG, r"\bplaybooks?\b", "mentions playbooks"),
        (_STRONG, r"\bknowledge[- ]base\b", "mentions the knowledge base"),
        (_STRONG, r"\bskill (registry|library)\b", "mentions the skill registry"),
        (_WEAK, r"\bcanon\b", "mentions canon"),
        (_WEAK, r"\bprompt (library|bank)\b", "mentions the prompt library"),
    )),
    "metrics": _compile((
        (_STRONG, r"\bmetabase\b", "mentions Metabase"),
        (_STRONG, r"\b(metrics|kpis?|analytics|dashboards?)\b", "asks about metrics"),
        (_STRONG, r"\bconversion rates?\b", "asks about conversion rates"),
        (_STRONG, r"\bhow many (of our )?(nurses|users|leads|applications|sign-?ups|readers|subscribers)\b", "asks for our counts"),
        (_STRONG, r"\b(our|the) (funnel numbers|lead volume|sign-?ups|retention)\b", "asks about our numbers"),
        (_WEAK, r"\b(conversion|retention|leads|funnel)\b", "uses funnel words"),
        (_WEAK, r"\bwe track\b", "asks what we track"),
    )),
    "media": _compile((
        (_STRONG, r"\bcloudinary\b", "mentions Cloudinary"),
        (_STRONG, r"\bmedia library\b", "mentions the media library"),
        (_STRONG, r"\bour (images?|photos?|videos?|media|assets?)\b", "asks about our media"),
        (_WEAK, r"\bassets?\b", "mentions assets"),
    )),
    "audience": _compile((
        (_STRONG, r"\ballnurses\b", "mentions allnurses"),
        (_STRONG, r"\br/(nursing|studentnurse)\b", "mentions nursing subreddits"),
        (_STRONG, r"\bnurses (say|complain|vent|feel|actually)\b", "asks what nurses say"),
        (_STRONG, r"\bwhat (do )?nurses (want|think|say|feel)\b", "asks what nurses want"),
        (_STRONG, r"\bvoice[- ]of[- ]nurse\b", "mentions voice-of-nurse"),
        (_WEAK, r"\b(reddit|forums?)\b", "mentions forums"),
        (_WEAK, r"\bcomplaints?\b", "mentions complaints"),
    )),
    "recruiting": _compile((
        (_STRONG, r"\bnursingmastery\.com\b", "mentions nursingmastery.com"),
        (_STRONG, r"\bnurse recruiting\b", "mentions nurse recruiting"),
        (_STRONG, r"\bcontent gaps?\b", "asks about content gaps"),
        (_STRONG, r"\bour (content|articles|briefing)\b", "asks about our content"),
        (_WEAK, r"\brecruit(ing|ers?)\b", "mentions recruiting"),
        (_WEAK, r"\bnursing mastery\b", "mentions Nursing Mastery"),
    )),
}


def _env_flag_disabled(name: str) -> bool:
    return os.getenv(name, "1").strip().lower() in {"0", "false", "off", "no"}


def _tiebreak_model() -> str:
    explicit = os.getenv("HLT_SCOPE_INFERENCE_MODEL")
    if explicit:
        return explicit
    fast = os.getenv("FAST_LLM", "")
    if fast.startswith("openai:"):
        return fast.split(":", 1)[1]
    return "gpt-4o-mini"


def _llm_tiebreak(task: str, candidates: list[str]) -> set[str] | None:
    """One fast-model call deciding which weak-signal scopes the query needs.

    Returns None (caller falls back to heuristics-only, dropping weak
    candidates) when disabled, unconfigured, or on any failure.
    """

    if _env_flag_disabled("HLT_SCOPE_INFERENCE_LLM"):
        return None
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or not candidates:
        return None

    descriptions = {
        "codebase": "the HLT estate code repositories (nursing-mastery, ScraperVault, katailyst2, MMM2, EBB)",
        "cms": "the Katailyst2 registry: playbooks, skills, prompts, knowledge bases",
        "metrics": "internal business metrics and analytics dashboards",
        "media": "the internal Cloudinary media library",
        "audience": "what nurses actually say on forums plus the internal voice-of-nurse corpus",
        "recruiting": "the nursingmastery.com content inventory and nurse-recruiting strategy corpus",
    }
    candidate_lines = "\n".join(f"- {key}: {descriptions.get(key, key)}" for key in candidates)
    prompt = (
        "You route research queries for an internal research tool. Decide which "
        "internal context scopes this query genuinely needs beyond public web "
        "research.\n\n"
        f"Query: {task}\n\n"
        f"Candidate scopes:\n{candidate_lines}\n\n"
        'Respond with JSON like {"scopes": ["codebase"]}. Only include candidates '
        "the query clearly benefits from; return an empty list when public web "
        "research alone is enough."
    )

    base_url = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    payload = json.dumps(
        {
            "model": _tiebreak_model(),
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 120,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_LLM_TIMEOUT_SECONDS) as response:  # noqa: S310 - configured LLM host
            body = json.loads(response.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        scopes = parsed.get("scopes")
        if not isinstance(scopes, list):
            return None
        return {scope for scope in scopes if scope in candidates}
    except Exception as error:  # noqa: BLE001 - tiebreak must never break a run
        logger.warning("Scope-inference LLM tiebreak failed: %s", type(error).__name__)
        return None


def infer_research_scope(
    task: str,
    *,
    readiness: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Infer which internal scopes `task` needs.

    `readiness` maps scope key -> integration status ("ready" / "partial" /
    "unavailable"); scopes without a usable integration are reported under
    `skipped_unready` instead of being activated, so a degraded backend is
    never auto-triggered.
    """

    result: dict[str, Any] = {
        "scopes": [],
        "reasons": {},
        "candidates": [],
        "llm_used": False,
        "skipped_unready": [],
    }
    if not task or not task.strip() or _env_flag_disabled("HLT_SCOPE_INFERENCE"):
        return result

    selected: dict[str, list[str]] = {}
    candidates: dict[str, list[str]] = {}
    for scope, signals in _SIGNALS.items():
        score = 0
        reasons: list[str] = []
        for weight, pattern, reason in signals:
            if pattern.search(task):
                score += weight
                reasons.append(reason)
        if score >= _STRONG:
            selected[scope] = reasons
        elif score == _WEAK:
            candidates[scope] = reasons
    result["candidates"] = sorted(candidates)

    if candidates:
        approved = _llm_tiebreak(task, sorted(candidates))
        if approved is not None:
            result["llm_used"] = True
            for scope in approved:
                selected[scope] = candidates[scope] + ["confirmed by LLM tiebreak"]

    if readiness is not None:
        for scope in list(selected):
            if readiness.get(scope) not in {"ready", "partial"}:
                result["skipped_unready"].append(scope)
                selected.pop(scope)
        result["skipped_unready"].sort()

    result["scopes"] = [scope for scope in INFERABLE_SCOPES if scope in selected]
    result["reasons"] = {scope: selected[scope] for scope in result["scopes"]}
    return result
