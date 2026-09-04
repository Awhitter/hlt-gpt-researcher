"""Pin the real scheduled-budget call sites; no model or test suite is run."""
from __future__ import annotations

import ast
import sys
from pathlib import Path


def assert_scheduled_run_budget(root: Path) -> None:
    scheduler = ast.parse((root / "cron/scheduler.py").read_text())
    loop = ast.parse((root / "agent/conversation_loop.py").read_text())
    transport = ast.parse((root / "agent/codex_runtime.py").read_text())
    context = ast.parse((root / "agent/turn_context.py").read_text())
    constructor = next(
        node for node in ast.walk(scheduler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "AIAgent"
    )
    assert any(
        keyword.arg is None and isinstance(keyword.value, ast.Call)
        and isinstance(keyword.value.func, ast.Name)
        and keyword.value.func.id == "budget_kwargs"
        for keyword in constructor.keywords
    ), "Cron AIAgent must receive the opt-in native run budget"
    fallback = next(keyword.value for keyword in constructor.keywords if keyword.arg == "fallback_model")
    assert isinstance(fallback, ast.IfExp)
    assert 'job.get(\'hlt_run_budget\') is not None' == ast.unparse(fallback.test)
    assert isinstance(fallback.body, ast.Constant) and fallback.body.value is None
    assert isinstance(fallback.orelse, ast.Name) and fallback.orelse.id == "fallback_model"
    assert any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "attach_budget" for node in ast.walk(scheduler)
    ), "Cron must attach cumulative accounting before running"
    loop_gate = next(
        node for node in ast.walk(loop)
        if isinstance(node, ast.While) and "api_call_count" in ast.unparse(node.test)
    )
    assert "admit_iteration(agent, messages, api_call_count)" in ast.unparse(loop_gate.body[1])
    assert any(isinstance(node, ast.Break) for node in ast.walk(loop_gate.body[1]))
    assert "failed = True" in ast.unparse(loop_gate.body[1]), "Budget exhaustion must fail the job"
    assert any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "admit_request" for node in ast.walk(loop_gate)
    ), "Actual requests, including inner retries, must keep their output cap"
    request_gate = next(
        node for node in ast.walk(loop_gate)
        if isinstance(node, ast.If) and "admit_request(agent, api_kwargs)" in ast.unparse(node.test)
    )
    assert "failed = True" in ast.unparse(request_gate), "Request budget failure must not become an empty success"
    stream_retries = next(
        node.value for node in ast.walk(transport)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "max_stream_retries" for target in node.targets)
    )
    assert isinstance(stream_retries, ast.IfExp)
    assert ast.unparse(stream_retries.test) == "getattr(agent, '_hlt_scheduled_budget', None) is not None"
    assert stream_retries.body.value == 0 and stream_retries.orelse.value == 1
    hook = next(
        node for node in ast.walk(context)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_invoke_hook" and node.args
        and isinstance(node.args[0], ast.Constant) and node.args[0].value == "pre_llm_call"
    )
    assert any(
        keyword.arg == "scheduled_run_budget"
        and ast.unparse(keyword.value) == "getattr(agent, '_hlt_scheduled_budget', None) is not None"
        for keyword in hook.keywords
    ), "Budgeted jobs must not launch an extra unaccounted wishing-well draw"


if __name__ == "__main__":
    assert_scheduled_run_budget(Path(sys.argv[1]))
