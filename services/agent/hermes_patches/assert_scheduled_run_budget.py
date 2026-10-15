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
    assert ast.unparse(fallback.orelse) == "setup.fallback_model"
    assert any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "attach_budget" for node in ast.walk(scheduler)
    ), "Cron must attach cumulative accounting before running"
    loop_gate = next(
        node for node in ast.walk(loop)
        if isinstance(node, ast.While) and "api_call_count" in ast.unparse(node.test)
    )
    iteration_gate = next(n for n in ast.walk(loop_gate) if isinstance(n, ast.If)
                          and "admit_iteration(agent, s.messages, s.api_call_count)" in ast.unparse(n.test))
    assert any(isinstance(node, ast.Break) for node in ast.walk(iteration_gate))
    assert "s.failed = True" in ast.unparse(iteration_gate), "Budget exhaustion must fail the job"
    request_owner = ast.parse((root / "agent/turn_api_request.py").read_text())
    request_gate = next(n for n in ast.walk(request_owner) if isinstance(n, ast.If)
                       and "admit_request(agent, api_kwargs)" in ast.unparse(n.test))
    assert any(isinstance(n, ast.Raise) for n in ast.walk(request_gate)), "Rejected request cannot become empty success"
    assert "agent.interrupt" in ast.unparse(request_gate), "Budget exhaustion must stop the native loop"
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
