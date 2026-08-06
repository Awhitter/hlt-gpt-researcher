"""FastMCP tool wrappers around `gpt_researcher.agent.GPTResearcher`.

Tool names and stateful flow intentionally match the upstream
assafelovic/gptr-mcp project so MCP clients configured for that server can
point at the hosted HLT endpoint with minimal changes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from gpt_researcher import GPTResearcher
from gpt_researcher.research_run_store import get_outputs_dir, get_research_run_store
from gpt_researcher.utils.enum import Tone

logger = logging.getLogger(__name__)

STORE_TTL_SECONDS = 60 * 60
STORE_MAX_ITEMS = 32

_VALID_SCOPE_KEYS = (
    "codebase",
    "cms",
    "qbank",
    "metrics",
    "firecrawl",
    "media",
    "audience",
    "recruiting",
)
_VALID_DEPTHS = ("fast", "balanced", "deep")


def _build_research_scope(scope: str | list[str] | None, depth: str) -> dict[str, Any]:
    """Translate the MCP tool's scope/depth params into an HLT research scope."""

    normalized_depth = depth if depth in _VALID_DEPTHS else "balanced"
    if scope is None or scope == "auto":
        return {"auto": True, "depth": normalized_depth}
    if scope == "none":
        return {"depth": normalized_depth}
    if isinstance(scope, str):
        scope = [scope]
    keys = [key for key in scope if key in _VALID_SCOPE_KEYS]
    return {**{key: True for key in keys}, "depth": normalized_depth}


def _prepare_scoped_request(
    query: str,
    research_scope: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], str | None, str | None, dict[str, Any] | None]:
    """Run the shared HLT scope pipeline (inference, presets, instructions).

    Falls back to a plain web request when the backend module is unavailable,
    so the MCP service never hard-fails on scope plumbing.
    """

    try:
        from backend.server.hlt_extensions import prepare_research_request
    except Exception as error:  # noqa: BLE001 - degraded mode is better than no research
        logger.warning("HLT scope pipeline unavailable: %s", type(error).__name__)
        return query, [], None, None, None

    try:
        task, _mcp_enabled, mcp_strategy, configs, metadata, scraper_override = (
            prepare_research_request(
                task=query,
                mcp_enabled=False,
                mcp_strategy="fast",
                mcp_configs=[],
                research_scope=research_scope,
            )
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("HLT scope resolution failed: %s", type(error).__name__, exc_info=True)
        return query, [], None, None, None
    return task, configs, mcp_strategy if configs else None, scraper_override, metadata


def _scope_summary(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Browser/agent-safe subset of the HLT scope metadata for tool responses."""

    if not metadata:
        return None
    auto = metadata.get("auto_scope") or {}
    return {
        "active_sources": metadata.get("active_sources", []),
        "degraded_sources": metadata.get("degraded_sources", []),
        "auto": {
            "requested": auto.get("requested", False),
            "applied": auto.get("applied", []),
            "reasons": auto.get("reasons", {}),
        },
        "mcp_server_count": metadata.get("mcp_server_count", 0),
        "depth": metadata.get("depth"),
    }


@dataclass
class StoredResearch:
    """One in-memory research session for follow-up MCP tool calls."""

    researcher: GPTResearcher
    query: str
    report_type: str
    report_source: str
    tone: str
    context: Any
    sources: list[dict[str, Any]]
    source_urls: list[str]
    created_at: float = field(default_factory=time.time)
    last_accessed_at: float = field(default_factory=time.time)


_research_by_id: dict[str, StoredResearch] = {}
_resource_by_topic: dict[str, str] = {}
_store_lock = asyncio.Lock()


def _resolve_tone(tone_str: str | None) -> Tone:
    if not tone_str:
        return Tone.Objective
    try:
        return Tone(tone_str)
    except ValueError:
        for tone in Tone:
            if tone.name.lower() == tone_str.lower() or tone.value.lower() == tone_str.lower():
                return tone
        logger.warning("Unknown tone %r; defaulting to Objective", tone_str)
        return Tone.Objective


def _success(data: dict[str, Any]) -> dict[str, Any]:
    return {"status": "success", **data}


def _error(message: str) -> dict[str, Any]:
    return {"status": "error", "message": message}


def _jsonable(value: Any) -> Any:
    """Return a JSON-compatible value without losing too much source context."""

    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(v) for v in value]
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _format_sources_for_response(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    formatted: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            formatted.append({"title": str(source), "url": "", "content_length": 0})
            continue
        formatted.append(
            {
                "title": source.get("title", "Unknown"),
                "url": source.get("url", ""),
                "content_length": len(source.get("content", "") or ""),
            }
        )
    return formatted


def _format_context_with_sources(topic: str, context: Any, sources: list[dict[str, Any]]) -> str:
    context_text = context if isinstance(context, str) else json.dumps(_jsonable(context), indent=2)
    lines = [f"## Research: {topic}", "", context_text, "", "## Sources:"]
    for index, source in enumerate(sources, start=1):
        if isinstance(source, dict):
            lines.append(f"{index}. {source.get('title', 'Unknown')}: {source.get('url', '')}")
        else:
            lines.append(f"{index}. {source}")
    return "\n".join(lines)


def _result_count(results: Any) -> int:
    if results is None:
        return 0
    if isinstance(results, str):
        return 1 if results else 0
    try:
        return len(results)
    except TypeError:
        return 1


def _topic_last_accessed(topic: str) -> float:
    """Recency of the research a topic points at; 0.0 once it is orphaned.

    `_resource_by_topic` maps topic -> research_id, so the value is a plain
    string. Ordering topics therefore has to go through `_research_by_id`.
    """
    item = _research_by_id.get(_resource_by_topic[topic])
    return item.last_accessed_at if item else 0.0


async def _prune_locked(now: float | None = None) -> None:
    now = now or time.time()
    expired_ids = [
        research_id
        for research_id, item in _research_by_id.items()
        if now - item.last_accessed_at > STORE_TTL_SECONDS
    ]
    for research_id in expired_ids:
        _research_by_id.pop(research_id, None)

    # Trim by size BEFORE sweeping topics. The other order orphans a topic in
    # the same pass that drops its research, leaving _resource_by_topic one
    # entry above the cap while _research_by_id sits at it — which used to
    # reach the topic trim below and call .last_accessed_at on a str.
    while len(_research_by_id) > STORE_MAX_ITEMS:
        oldest_id = min(_research_by_id, key=lambda k: _research_by_id[k].last_accessed_at)
        _research_by_id.pop(oldest_id, None)

    # Any topic whose research is gone — expired or trimmed — goes with it.
    # This subsumes the TTL check: TTL-expired ids were already popped above.
    orphaned_topics = [
        topic
        for topic, research_id in _resource_by_topic.items()
        if research_id not in _research_by_id
    ]
    for topic in orphaned_topics:
        _resource_by_topic.pop(topic, None)

    while len(_resource_by_topic) > STORE_MAX_ITEMS:
        _resource_by_topic.pop(min(_resource_by_topic, key=_topic_last_accessed), None)


async def _store_research(research_id: str, item: StoredResearch, *, resource_topic: str | None = None) -> None:
    async with _store_lock:
        await _prune_locked()
        _research_by_id[research_id] = item
        if resource_topic:
            _resource_by_topic[resource_topic] = research_id


async def _get_research(research_id: str) -> StoredResearch | None:
    async with _store_lock:
        await _prune_locked()
        item = _research_by_id.get(research_id)
        if item:
            item.last_accessed_at = time.time()
        return item


async def _get_resource_topic(topic: str) -> StoredResearch | None:
    async with _store_lock:
        await _prune_locked()
        research_id = _resource_by_topic.get(topic)
        item = _research_by_id.get(research_id) if research_id else None
        if item:
            item.last_accessed_at = time.time()
        return item


def clear_hot_cache() -> None:
    _research_by_id.clear()
    _resource_by_topic.clear()


def _stored_research_from_run(run: dict[str, Any]) -> StoredResearch:
    researcher = GPTResearcher(
        query=run["query"],
        report_type=run.get("report_type") or "research_report",
        report_source=run.get("report_source") or "web",
        tone=_resolve_tone(run.get("tone")),
    )
    researcher.context = run.get("context") or []
    researcher.research_sources = run.get("sources") or []
    researcher.visited_urls = set(run.get("source_urls") or [])
    return StoredResearch(
        researcher=researcher,
        query=run["query"],
        report_type=run.get("report_type") or "research_report",
        report_source=run.get("report_source") or "web",
        tone=run.get("tone") or "Objective",
        context=run.get("context") or [],
        sources=run.get("sources") or [],
        source_urls=run.get("source_urls") or [],
    )


async def _get_research_or_persisted(research_id: str) -> tuple[StoredResearch | None, dict[str, Any] | None]:
    item = await _get_research(research_id)
    if item:
        return item, None

    run = get_research_run_store().get_run(research_id)
    if not run:
        return None, None
    if run.get("status") != "completed":
        return None, run

    item = _stored_research_from_run(run)
    await _store_research(research_id, item, resource_topic=run.get("resource_topic"))
    return item, run


def _safe_report_filename(research_id: str) -> str:
    return re.sub(r"[^\w\s-]", "", Path(research_id).name).strip() or str(uuid.uuid4())


async def _write_report_markdown(report: str, research_id: str) -> str:
    output_dir = get_outputs_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"{_safe_report_filename(research_id)[:60]}.md"
    await asyncio.to_thread(report_path.write_text, report, "utf-8")
    return str(report_path)


async def _conduct_research(
    query: str,
    *,
    report_type: str = "research_report",
    report_source: str = "web",
    tone: str = "Objective",
    scope: str | list[str] | None = "auto",
    depth: str = "balanced",
) -> tuple[StoredResearch, dict[str, Any] | None]:
    research_scope = _build_research_scope(scope, depth)
    task, mcp_configs, mcp_strategy, scraper_override, scope_metadata = await asyncio.to_thread(
        _prepare_scoped_request, query, research_scope
    )
    researcher = GPTResearcher(
        query=task,
        report_type=report_type,
        report_source=report_source,
        tone=_resolve_tone(tone),
        mcp_configs=mcp_configs or None,
        mcp_strategy=mcp_strategy,
    )
    if scraper_override:
        researcher.cfg.scraper = scraper_override
    await researcher.conduct_research()
    return (
        StoredResearch(
            researcher=researcher,
            query=query,
            report_type=report_type,
            report_source=report_source,
            tone=tone,
            context=_jsonable(researcher.get_research_context()),
            sources=_jsonable(researcher.get_research_sources()),
            source_urls=list(researcher.get_source_urls()),
        ),
        scope_metadata,
    )


async def research_resource_tool(topic: str) -> str:
    cached = await _get_resource_topic(topic)
    if cached:
        return _format_context_with_sources(topic, cached.context, cached.sources)

    persisted = get_research_run_store().get_run_by_resource_topic(topic)
    if persisted and persisted.get("status") == "completed":
        item = _stored_research_from_run(persisted)
        await _store_research(persisted["research_id"], item, resource_topic=topic)
        return _format_context_with_sources(topic, item.context, item.sources)

    logger.info("Conducting resource research for topic=%r", topic)
    research_id = str(uuid.uuid4())
    store = get_research_run_store()
    store.create_run(
        research_id,
        query=topic,
        report_type="research_report",
        report_source="web",
        tone="Objective",
        status="running",
        resource_topic=topic,
    )
    try:
        item, _scope_metadata = await _conduct_research(topic)
        await _store_research(research_id, item, resource_topic=topic)
        store.complete_run(
            research_id,
            context=item.context,
            sources=item.sources,
            source_urls=item.source_urls,
            costs=item.researcher.get_costs(),
        )
        return _format_context_with_sources(topic, item.context, item.sources)
    except Exception as exc:
        store.fail_run(research_id, error_code="runtime_error", error_message=str(exc))
        raise


async def deep_research_tool(
    query: str,
    report_type: str = "research_report",
    report_source: str = "web",
    tone: str = "Objective",
    scope: str | list[str] | None = "auto",
    depth: str = "balanced",
) -> dict[str, Any]:
    research_id = str(uuid.uuid4())
    store = get_research_run_store()
    store.create_run(
        research_id,
        query=query,
        report_type=report_type,
        report_source=report_source,
        tone=tone,
        status="running",
        resource_topic=query,
    )
    try:
        logger.info("Conducting deep research for research_id=%s query=%r", research_id, query)
        item, scope_metadata = await _conduct_research(
            query,
            report_type=report_type,
            report_source=report_source,
            tone=tone,
            scope=scope,
            depth=depth,
        )
        await _store_research(research_id, item, resource_topic=query)
        store.complete_run(
            research_id,
            context=item.context,
            sources=item.sources,
            source_urls=item.source_urls,
            costs=item.researcher.get_costs(),
            hlt_research_scope=scope_metadata,
        )
        return _success(
            {
                "research_id": research_id,
                "query": query,
                "source_count": len(item.sources),
                "context": item.context,
                "sources": _format_sources_for_response(item.sources),
                "source_urls": item.source_urls,
                "hlt_scope": _scope_summary(scope_metadata),
            }
        )
    except Exception as exc:
        store.fail_run(research_id, error_code="runtime_error", error_message=str(exc))
        logger.error("deep_research failed for query=%r: %s", query, exc, exc_info=True)
        return _error(str(exc))


async def write_report_tool(research_id: str, custom_prompt: str | None = None) -> dict[str, Any]:
    item, persisted = await _get_research_or_persisted(research_id)
    if item is None:
        if persisted:
            return _error(f"Research ID is not completed; current status is {persisted.get('status')}.")
        return _error("Research ID not found. Please conduct research first.")

    try:
        logger.info("Writing report for research_id=%s", research_id)
        report = await item.researcher.write_report(custom_prompt=custom_prompt or "")
        md_path = await _write_report_markdown(report, research_id)
        item.context = _jsonable(item.researcher.get_research_context())
        item.sources = _jsonable(item.researcher.get_research_sources()) or item.sources
        item.source_urls = list(item.researcher.get_source_urls()) or item.source_urls
        get_research_run_store().complete_run(
            research_id,
            context=item.context,
            sources=item.sources,
            source_urls=item.source_urls,
            costs=item.researcher.get_costs(),
            report_path=md_path,
            md_path=md_path,
        )
        return _success(
            {
                "research_id": research_id,
                "report": report,
                "source_count": len(item.sources),
                "costs": item.researcher.get_costs(),
                "report_path": md_path,
                "md_path": md_path,
            }
        )
    except Exception as exc:
        logger.error("write_report failed for research_id=%s: %s", research_id, exc, exc_info=True)
        get_research_run_store().fail_run(research_id, error_code="runtime_error", error_message=str(exc))
        return _error(str(exc))


async def get_research_sources_tool(research_id: str) -> dict[str, Any]:
    item, persisted = await _get_research_or_persisted(research_id)
    if item is None:
        if persisted:
            return _error(f"Research ID is not completed; current status is {persisted.get('status')}.")
        return _error("Research ID not found. Please conduct research first.")
    return _success(
        {
            "research_id": research_id,
            "sources": _format_sources_for_response(item.sources),
            "source_urls": item.source_urls,
        }
    )


async def get_research_context_tool(research_id: str) -> dict[str, Any]:
    item, persisted = await _get_research_or_persisted(research_id)
    if item is None:
        if persisted:
            return _error(f"Research ID is not completed; current status is {persisted.get('status')}.")
        return _error("Research ID not found. Please conduct research first.")
    return _success({"research_id": research_id, "context": item.context})


def register_tools(mcp: FastMCP) -> None:
    """Register GPT Researcher MCP tools, resource, and prompt."""

    @mcp.resource("research://{topic}")
    async def research_resource(topic: str) -> str:
        """Return cached or newly generated research context for a topic."""
        return await research_resource_tool(topic)

    @mcp.tool()
    async def deep_research(
        query: str,
        report_type: str = "research_report",
        report_source: str = "web",
        tone: str = "Objective",
        scope: str | list[str] | None = "auto",
        depth: str = "balanced",
    ) -> dict[str, Any]:
        """Conduct deep, cited research and return a research_id for follow-up calls.

        With scope="auto" (the default) this routes to internal HLT context when
        the query is about it, and stays pure public-web research otherwise. It
        can reach: the estate code repos — nursing-mastery (nurse-facing
        frontend), ScraperVault (nurse-recruiting backend), katailyst2 (AI
        primitives + registry), MMM2 (multimedia), EBB (metrics) — plus the
        Katailyst2 registry (playbooks, skills, knowledge bases), internal
        business metrics, the Cloudinary media library, and the nurse
        audience/recruiting corpora. Prefer this tool over quick_search for any
        question about those systems, our code, our content, or our numbers.

        scope: "auto" infers relevant internal scopes from the query; pass a
        list such as ["codebase", "cms"] to pin scopes (valid keys: codebase,
        cms, qbank, metrics, firecrawl, media, audience, recruiting); pass
        "none" to force pure web research.
        depth: "fast" | "balanced" | "deep".
        """

        return await deep_research_tool(
            query, report_type, report_source, tone, scope=scope, depth=depth
        )

    @mcp.tool()
    async def quick_search(
        query: str,
        summary: bool = True,
        domains: list[str] | None = None,
        scope: str | list[str] | None = "auto",
        depth: str = "fast",
    ) -> dict[str, Any]:
        """Fast lookup with the same auto-scope router as deep_research.

        scope="auto" (default) infers HLT estate context when the query needs
        it — nursing-mastery, ScraperVault, katailyst2, MMM2, EBB, the
        Katailyst2 registry, metrics, media, audience/recruiting — and stays
        pure public-web search otherwise. Prefer deep_research for a full
        cited report; use this for a quick answer.

        scope: "auto" | list of keys (codebase, cms, qbank, metrics,
        firecrawl, media, audience, recruiting) | "none" for forced web-only.
        depth: "fast" | "balanced" | "deep" (default "fast").
        """

        search_id = str(uuid.uuid4())
        research_scope = _build_research_scope(scope, depth)
        task, mcp_configs, mcp_strategy, scraper_override, scope_metadata = (
            await asyncio.to_thread(_prepare_scoped_request, query, research_scope)
        )
        try:
            # Estate scopes need MCP presets; escalate to a short research pass
            # so Katailyst2/GitHub context actually fires. Pure-web stays cheap.
            if mcp_configs:
                logger.info(
                    "quick_search escalating to scoped research for search_id=%s query=%r scopes=%s",
                    search_id,
                    query,
                    (scope_metadata or {}).get("auto_scope", {}).get("applied")
                    or (scope_metadata or {}).get("active_sources"),
                )
                item, scope_metadata = await _conduct_research(
                    query,
                    scope=scope,
                    depth=depth if depth in _VALID_DEPTHS else "fast",
                )
                return _success(
                    {
                        "search_id": search_id,
                        "query": query,
                        "result_count": len(item.sources),
                        "search_results": _format_sources_for_response(item.sources),
                        "context": item.context,
                        "hlt_scope": _scope_summary(scope_metadata),
                        "mode": "scoped_research",
                    }
                )

            logger.info("Performing quick search for search_id=%s query=%r", search_id, query)
            researcher = GPTResearcher(
                query=task,
                report_type="research_report",
                mcp_configs=None,
                mcp_strategy=mcp_strategy,
            )
            if scraper_override:
                researcher.cfg.scraper = scraper_override
            results = await researcher.quick_search(
                query=task,
                query_domains=domains,
                aggregated_summary=summary,
            )
            return _success(
                {
                    "search_id": search_id,
                    "query": query,
                    "result_count": _result_count(results),
                    "search_results": _jsonable(results),
                    "hlt_scope": _scope_summary(scope_metadata),
                    "mode": "web",
                }
            )
        except Exception as exc:
            logger.error("quick_search failed for query=%r: %s", query, exc, exc_info=True)
            return _error(str(exc))

    @mcp.tool()
    async def write_report(research_id: str, custom_prompt: str | None = None) -> dict[str, Any]:
        """Generate a report from a previous deep_research research_id."""

        return await write_report_tool(research_id, custom_prompt)

    @mcp.tool()
    async def get_research_sources(research_id: str) -> dict[str, Any]:
        """Return the sources used in a previous deep_research run."""

        return await get_research_sources_tool(research_id)

    @mcp.tool()
    async def get_research_context(research_id: str) -> dict[str, Any]:
        """Return the full context from a previous deep_research run."""

        return await get_research_context_tool(research_id)

    @mcp.prompt()
    def research_query(topic: str, goal: str, report_format: str = "research_report") -> str:
        """Create an MCP prompt explaining how to use GPT Researcher tools."""

        return (
            f"Please research the following topic: {topic}\n\n"
            f"Goal: {goal}\n\n"
            "Use research://{topic} for direct context when appropriate, or call "
            "deep_research to get a research_id for follow-up calls. After deep_research, "
            f"use write_report with a custom prompt to generate a structured {report_format}."
        )

    logger.info(
        "Registered GPT Researcher MCP tools: deep_research, quick_search, "
        "write_report, get_research_sources, get_research_context"
    )
