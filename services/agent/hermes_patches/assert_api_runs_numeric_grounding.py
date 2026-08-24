"""Build-time contract check for HLT's patched Hermes ``/v1/runs`` seam."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


class _FakeAgent:
    def __init__(
        self,
        callback: Any,
        final_response: str,
        *,
        tool_result: Any | None = None,
    ) -> None:
        self.callback = callback
        self.final_response = final_response
        self.tool_result = tool_result or {
            "steps": [
                {"name": "search", "count": 975},
                {"name": "detail", "count": 37},
                {"name": "apply", "count": 6},
                {"name": "received", "count": 2},
            ]
        }
        self.session_prompt_tokens = 10
        self.session_completion_tokens = 5
        self.session_total_tokens = 15
        self.closed = False

    def run_conversation(self, **_: Any) -> dict[str, str]:
        self.callback(
            "tool.completed",
            "mcp__posthog__exec",
            None,
            None,
            result=self.tool_result,
            is_error=False,
            duration=0.01,
        )
        return {"final_response": self.final_response}

    def close(self) -> None:
        self.closed = True


class _FakeRequest:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.headers: dict[str, str] = {}

    async def json(self) -> dict[str, Any]:
        return self.payload


async def _wait_for_terminal(adapter: Any, run_id: str) -> dict[str, Any]:
    for _ in range(200):
        status = adapter._run_statuses[run_id]
        if status.get("status") in {"completed", "failed", "cancelled"}:
            return status
        await asyncio.sleep(0.01)
    raise AssertionError(f"run did not terminalize: {run_id}")


async def _assert_live_run_seam() -> None:
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))

    fabricated = _FakeAgent(
        None,
        "Funnel: search 168 → detail 159 → apply 18 → received 2.",
    )
    corrected = _FakeAgent(
        None,
        """funnel_search_performed — 975 people, conversion 100%, drop-off 0%
funnel_job_viewed — 37 people, conversion 3.79%, drop-off 96.21%
funnel_profile_milestone_reached — 6 people, conversion 0.62%, drop-off 99.38%
funnel_application_submitted — 2 people, conversion 0.21%, drop-off 99.79%""",
        tool_result=(
            '<untrusted_tool_result source="mcp__posthog__exec">\n'
            '{"result":"Metric|funnel_search_performed|funnel_job_viewed|'
            'funnel_profile_milestone_reached|funnel_application_submitted\\n'
            'Total person count|975|37|6|2\\n'
            'Conversion rate|100%|3.79%|0.62%|0.21%\\n'
            'Dropoff rate|0%|96.21%|99.38%|99.79%"}\n'
            "</untrusted_tool_result>"
        ),
    )
    pending = [fabricated, corrected]

    def create_agent(**kwargs: Any) -> _FakeAgent:
        agent = pending.pop(0)
        agent.callback = kwargs["tool_progress_callback"]
        return agent

    adapter._create_agent = create_agent
    failed_response = await adapter._handle_runs(_FakeRequest({"input": "Read the funnel."}))
    assert failed_response.status == 202
    failed_run = json.loads(failed_response.text)["run_id"]
    failed = await _wait_for_terminal(adapter, failed_run)
    assert failed["status"] == "failed"
    assert "output" not in failed
    assert failed["grounding"]["status"] == "failed"
    assert [item["value"] for item in failed["grounding"]["unsupported"]] == [
        "168",
        "159",
        "18",
    ]
    assert fabricated.closed is True

    passed_response = await adapter._handle_runs(_FakeRequest({"input": "Read the funnel."}))
    assert passed_response.status == 202
    passed_run = json.loads(passed_response.text)["run_id"]
    passed = await _wait_for_terminal(adapter, passed_run)
    assert passed["status"] == "completed"
    assert passed["grounding"]["status"] == "passed"
    assert passed["output"] == corrected.final_response
    assert corrected.closed is True


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: assert_api_runs_numeric_grounding.py /opt/hermes")
    root = Path(sys.argv[1])
    source = (root / "gateway" / "platforms" / "api_server.py").read_text(
        encoding="utf-8"
    )
    tool_executor_source = (root / "agent" / "tool_executor.py").read_text(
        encoding="utf-8"
    )
    codex_runtime_source = (root / "agent" / "codex_runtime.py").read_text(
        encoding="utf-8"
    )
    compile(source, "gateway/platforms/api_server.py", "exec")

    assert "from hlt_numeric_grounding import NumericGroundingLedger" in source
    runs = _between(
        source,
        "    async def _handle_runs",
        "    async def _handle_get_run",
    )
    assert runs.index("NumericGroundingLedger(user_message)") < runs.index(
        "self._create_agent("
    )
    assert "numeric_grounding.observe_tool_event(" in runs
    assert 'event_type == "tool.completed"' not in runs, (
        "tool success/error filtering belongs to the owned deterministic ledger"
    )
    # Pin the real upstream producers, not just the handler fake above. Both
    # native executor paths expose the complete pre-persistence result, and the
    # Codex runtime exposes its completion payload, through the exact kwargs
    # consumed by NumericGroundingLedger.observe_tool_event().
    assert tool_executor_source.count("result=display_function_result,") >= 2
    assert 'cb("tool.completed", name, None, None,' in codex_runtime_source
    assert "duration=duration, is_error=is_error, result=result" in codex_runtime_source

    run_sync = _between(runs, "                def _run_sync():", "                result, usage =")
    assert run_sync.index("agent.run_conversation(") < run_sync.index("agent.close()")
    assert "return r, u" in run_sync

    completion = _between(
        runs,
        "                else:\n                    final_response =",
        "            except asyncio.CancelledError:",
    )
    assert completion.index("numeric_grounding.validate(final_response)") < completion.index(
        '"event": "run.completed"'
    )
    assert '"event": "run.failed"' in completion
    assert 'grounding=grounding_summary' in completion
    assert 'output=final_response' in completion
    asyncio.run(_assert_live_run_seam())


if __name__ == "__main__":
    main()
