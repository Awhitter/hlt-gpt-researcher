"""Build-time proof for stricter messaging budgets without shrinking API work."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any


def assert_platform_turn_budget(hermes_root: Path) -> None:
    path = hermes_root / "gateway" / "run.py"
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")
    tree = ast.parse(source, filename=str(path))
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_current_max_iterations_for_platform"
    )
    namespace: dict[str, Any] = {
        "Any": Any,
        "_current_max_iterations": lambda: 24,
    }
    exec(  # noqa: S102 - executes one selected function from pinned local source
        compile(ast.Module(body=[helper], type_ignores=[]), str(path), "exec"),
        namespace,
    )
    resolve = namespace["_current_max_iterations_for_platform"]

    config = {"agent": {"platform_max_turns": {"slack": 7}}}
    assert resolve(config, "slack") == 7
    assert resolve(config, "api_server") == 24
    assert resolve(
        {"agent": {"platform_max_turns": {"slack": 99}}}, "slack"
    ) == 24
    assert resolve(
        {"agent": {"platform_max_turns": {"slack": "broken"}}}, "slack"
    ) == 24

    call_sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_current_max_iterations_for_platform"
    ]
    assert len(call_sites) == 2


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: assert_platform_turn_budget.py HERMES_ROOT")
    assert_platform_turn_budget(Path(sys.argv[1]))
