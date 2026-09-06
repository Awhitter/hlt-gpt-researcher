"""Check the pinned API-to-native-loop wiring without starting a model."""
from __future__ import annotations

import ast
import sys
from pathlib import Path


def assert_hosted_turn_budget(root: Path) -> None:
    source = (root / "gateway/platforms/api_server_runs.py").read_text()
    tree = ast.parse(source)
    run = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_runs")
    calls = [n for n in ast.walk(run) if isinstance(n, ast.Call)]
    validate = next((n for n in calls if ast.unparse(n.func) == "validate_hosted_turn_limit"), None)
    assert validate is not None, "API must validate the explicit per-run ceiling before admission"
    assert ast.unparse(validate.args[0]) == "body.get('max_iterations')"
    attach = next((n for n in calls if ast.unparse(n.func) == "attach_hosted_turn_limit"), None)
    assert attach is not None, "API must attach the requested ceiling to its actual native agent"
    assert [ast.unparse(arg) for arg in attach.args] == ["agent", "max_turns"]
    create = next(n for n in calls if ast.unparse(n.func) == "self._create_agent")
    execute = next(n for n in calls if ast.unparse(n.func) == "agent.run_conversation")
    assert validate.lineno < create.lineno < attach.lineno < execute.lineno
    assert any(
        isinstance(n, ast.Try)
        and any(c is attach for statement in n.body for c in ast.walk(statement))
        and any(
            isinstance(c, ast.Call) and ast.unparse(c.func) == "agent.close"
            for statement in n.finalbody for c in ast.walk(statement)
        )
        for n in ast.walk(run)
    ), "A rejected runtime must still close the agent it created"


if __name__ == "__main__":
    assert_hosted_turn_budget(Path(sys.argv[1]))
