"""Eviction tests for the hosted MCP research store.

`_resource_by_topic` maps topic -> research_id, so its values are strings. The
size-trim loop ordered topics with `_resource_by_topic[k].last_accessed_at`,
which is `.last_accessed_at` on a `str`. It only fired once the topic map
outgrew the id map — which the old ordering caused by trimming ids *after*
sweeping topics, orphaning one in the same pass.

The cost was not a clean error: `deep_research` finished the entire billed
research pass and then raised inside `_store_research`, before
`store.complete_run`, so the context and sources were never persisted.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from mcp_server import tools

# Entries must look RECENT or the TTL sweep clears the store on every call and
# the size trim — where the bug lives — is never reached. An earlier version of
# this file used 1000.0 as a timestamp, i.e. 1970, and passed against the bug.
NOW = time.time()


def _fake_item(accessed_at: float) -> tools.StoredResearch:
    """A StoredResearch carrying only the fields eviction reads."""
    return tools.StoredResearch(
        researcher=None,
        query="q",
        report_type="research_report",
        report_source="web",
        tone="objective",
        context=None,
        sources=[],
        source_urls=[],
        created_at=accessed_at,
        last_accessed_at=accessed_at,
    )


def _fresh(i: int) -> tools.StoredResearch:
    """Item i, recent enough to survive TTL, ordered oldest-first by i."""
    return _fake_item(NOW + i)


@pytest.fixture(autouse=True)
def _clean_store():
    tools._research_by_id.clear()
    tools._resource_by_topic.clear()
    yield
    tools._research_by_id.clear()
    tools._resource_by_topic.clear()


def test_store_survives_well_past_the_cap():
    """Assert after EVERY store: the original bug alternated on/off.

    The ceiling is cap + 1, not cap: `_store_research` prunes *before* it
    inserts, so one entry above the cap between calls is by design. What must
    never happen is unbounded growth — or the AttributeError this used to
    raise on the 34th store, after the billed research pass had completed and
    before anything was persisted.
    """
    ceiling = tools.STORE_MAX_ITEMS + 1

    async def run():
        for i in range(tools.STORE_MAX_ITEMS + 12):
            await tools._store_research(
                f"id-{i}", _fresh(i), resource_topic=f"topic-{i}"
            )
            assert len(tools._research_by_id) <= ceiling
            assert len(tools._resource_by_topic) <= ceiling

    asyncio.run(run())


def test_no_topic_outlives_its_research():
    async def run():
        for i in range(tools.STORE_MAX_ITEMS + 5):
            await tools._store_research(
                f"id-{i}", _fresh(i), resource_topic=f"topic-{i}"
            )

        for topic, research_id in tools._resource_by_topic.items():
            assert research_id in tools._research_by_id, f"{topic} points at evicted research"

    asyncio.run(run())


def test_oldest_entries_are_the_ones_evicted():
    async def run():
        total = tools.STORE_MAX_ITEMS + 4
        for i in range(total):
            await tools._store_research(
                f"id-{i}", _fresh(i), resource_topic=f"topic-{i}"
            )

        kept = set(tools._research_by_id)
        assert f"id-{total - 1}" in kept, "most recent research was evicted"
        assert f"id-0" not in kept, "oldest research survived the cap"

    asyncio.run(run())


def test_ttl_expiry_takes_topics_with_it():
    async def run():
        await tools._store_research("old", _fresh(0), resource_topic="stale-topic")
        await tools._store_research("new", _fresh(500), resource_topic="fresh-topic")

        async with tools._store_lock:
            await tools._prune_locked(now=NOW + tools.STORE_TTL_SECONDS + 1)

        assert "old" not in tools._research_by_id
        assert "stale-topic" not in tools._resource_by_topic

    asyncio.run(run())
