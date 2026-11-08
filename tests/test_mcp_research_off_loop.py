"""A research pass must not freeze the MCP server's event loop.

On 8 Nov 2026 a code-scope quick_search held the loop long enough that Render's
5-second /health check failed and restarted hlt-research-mcp mid-answer, twice.
"""

from __future__ import annotations

import asyncio
import time

from mcp_server import tools


def test_a_blocking_research_pass_leaves_the_server_loop_answering():
    finished: list[float] = []

    async def blocking_research() -> str:
        time.sleep(0.6)  # stands in for synchronous compression or provider work
        finished.append(time.monotonic())
        return "done"

    async def scenario() -> tuple[str, list[float]]:
        ticks: list[float] = []

        async def health_probe() -> None:
            for _ in range(5):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)

        probe = asyncio.create_task(health_probe())
        result = await tools._off_server_loop(blocking_research)
        await probe
        return result, ticks

    result, ticks = asyncio.run(scenario())

    assert result == "done"
    assert len(ticks) == 5
    # The probes answered while the pass was still blocking its own thread.
    assert sum(tick < finished[0] for tick in ticks) >= 4


def test_research_errors_still_reach_the_caller():
    async def failing_research() -> None:
        raise RuntimeError("provider refused")

    async def scenario() -> None:
        await tools._off_server_loop(failing_research)

    try:
        asyncio.run(scenario())
    except RuntimeError as error:
        assert str(error) == "provider refused"
    else:  # pragma: no cover - the error must propagate
        raise AssertionError("the research error was swallowed")
