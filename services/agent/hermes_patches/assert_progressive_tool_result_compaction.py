"""Build-time proof for bounded progressive discovery and spill recovery."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_function(
    path: Path,
    function_name: str,
    *,
    assignments: set[str],
    namespace: dict,
):
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")
    tree = ast.parse(source, filename=str(path))
    body = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
        )
        or (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id in assignments
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
            )
        )
        or (isinstance(node, ast.FunctionDef) and node.name == function_name)
    ]
    exec(  # noqa: S102 - executes selected nodes from the pinned local source
        compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"),
        namespace,
    )
    return namespace[function_name]


def assert_progressive_result_contract(hermes_root: Path) -> None:
    long_description = "useful capability details " * 400
    definitions = [
        {
            "type": "function",
            "function": {
                "name": f"mcp__katailyst2__verb_{index}",
                "description": long_description,
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            },
        }
        for index in range(5)
    ]
    search_namespace = {
        "json": json,
        "classify_tools": lambda tool_defs: ([], list(tool_defs)),
        "_describe_classification": lambda _name: "not_found",
        "load_config_readonly": lambda: None,
        "tool_error": lambda message: json.dumps({"error": message}),
    }
    dispatch_tool_describe = _load_function(
        hermes_root / "tools" / "tool_search.py",
        "dispatch_tool_describe",
        assignments={
            "_MAX_DESCRIBE_NAMES_PER_CALL",
            "_MAX_DESCRIBE_NAMES_PER_RESPONSE",
            "_MAX_TOOL_DESCRIPTION_CHARS",
        },
        namespace=search_namespace,
    )
    payload = json.loads(
        dispatch_tool_describe(
            {"names": [item["function"]["name"] for item in definitions]},
            current_tool_defs=definitions,
        )
    )

    assert list(payload["tools"]) == [
        "mcp__katailyst2__verb_0",
        "mcp__katailyst2__verb_1",
        "mcp__katailyst2__verb_2",
    ]
    assert payload["deferred_names"] == [
        "mcp__katailyst2__verb_3",
        "mcp__katailyst2__verb_4",
    ]
    first = payload["tools"]["mcp__katailyst2__verb_0"]
    assert len(first["description"]) <= search_namespace[
        "_MAX_TOOL_DESCRIPTION_CHARS"
    ]
    assert first["description_compacted"] is True
    assert first["description_original_chars"] == len(long_description)
    assert first["parameters"] == definitions[0]["function"]["parameters"]

    storage_path = hermes_root / "tools" / "tool_result_storage.py"
    storage_namespace = {
        "PERSISTED_OUTPUT_TAG": "<persisted-output>",
        "PERSISTED_OUTPUT_CLOSING_TAG": "</persisted-output>",
        "hashlib": hashlib,
        "re": re,
    }
    session_prefix = _load_function(
        storage_path,
        "_session_spillover_prefix",
        assignments=set(),
        namespace=storage_namespace,
    )
    safe_result_filename = _load_function(
        storage_path,
        "_safe_result_filename",
        assignments={
            "_UNSAFE_RESULT_FILENAME_CHARS",
            "_MAX_RESULT_FILENAME_STEM",
        },
        namespace=storage_namespace,
    )
    expected_prefix = session_prefix("slack-session-a")
    assert safe_result_filename(
        "call/result", session_id="slack-session-a"
    ).startswith(f"{expected_prefix}_")
    assert not safe_result_filename(
        "call/result", session_id="slack-session-b"
    ).startswith(f"{expected_prefix}_")

    build_persisted_message = _load_function(
        storage_path,
        "_build_persisted_message",
        assignments=set(),
        namespace=storage_namespace,
    )
    persisted = build_persisted_message(
        "preview", True, 276_953, "/data/hermes/cache/spillover/call.txt"
    )
    assert "read_spillover" in persisted
    assert "re-requesting the same data" in persisted
    assert "execute_code" not in persisted
    assert "view:schema" in persisted

    # Exercise the real storage function, not just a string marker: a schema
    # preview must be complete while the exact original is still persisted.
    saved = {}
    storage_namespace.update({
        "DEFAULT_BUDGET": SimpleNamespace(preview_size=1500, resolve_threshold=lambda _name: 16000),
        "generate_preview": lambda content, max_chars: (content[:max_chars], True),
        "_write_to_spillover": lambda content, filename: saved.update(content=content, filename=filename) or "/data/hermes/cache/spillover/own.txt",
        "_is_host_side_env": lambda _env: True,
        "logger": SimpleNamespace(info=lambda *_args: None),
    })
    persist_result = _load_function(storage_path, "maybe_persist_tool_result", assignments=set(), namespace=storage_namespace)
    schema = {"type": "object", "required": ["days"], "properties": {"days": {"enum": [7, 28]}}}
    raw = json.dumps({"result": json.dumps({"detailLevel": "schema", "tool": {
        "name": "example", "inputSchema": schema, "outputSchema": {"description": "x" * 40000},
    }})})
    result = persist_result(raw, "mcp__katailyst2__tool_describe", "call", session_id="own")
    assert saved["content"] == raw
    assert saved["filename"].startswith(session_prefix("own") + "_")
    assert "Complete call/input schema" in result and len(result) < 10000
    assert '"days"' in result and "40000" not in result

    executor_source = (hermes_root / "agent" / "tool_executor.py").read_text(
        encoding="utf-8"
    )
    executor_tree = ast.parse(executor_source)
    persistence_calls = [
        node
        for node in ast.walk(executor_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"maybe_persist_tool_result", "enforce_turn_budget"}
    ]
    assert len(persistence_calls) == 5
    assert all(
        any(keyword.arg == "session_id" for keyword in node.keywords)
        for node in persistence_calls
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(
            "usage: assert_progressive_tool_result_compaction.py HERMES_ROOT"
        )
    assert_progressive_result_contract(Path(sys.argv[1]))
