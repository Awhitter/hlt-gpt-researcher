"""Small behavioral check of the pinned native assembly, without booting Hermes."""
from __future__ import annotations

import ast
import dataclasses
import json
import logging
import math
import sys
import types
from pathlib import Path
from unittest.mock import patch


def assert_always_loaded_tools(root: Path) -> None:
    path = root / "tools" / "tool_search.py"
    source = path.read_text()
    compile(source, str(path), "exec")
    selected = {
        "ToolSearchConfig", "AssemblyResult", "_safe_int", "_safe_float",
        "_core_tool_names", "is_deferrable_tool_name", "classify_tools",
        "estimate_tokens_from_schemas", "should_activate", "listing_token_budget",
        "assemble_tool_defs", "scoped_deferrable_names", "_describe_classification",
        "dispatch_tool_describe",
    }
    tree = ast.parse(source)
    nodes = [n for n in tree.body if
             (isinstance(n, ast.ImportFrom) and n.module == "__future__") or
             (isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in selected)]
    module = types.ModuleType("hlt_native_hot_tools_check")
    bridge_names = frozenset({"tool_search", "tool_describe", "tool_call"})
    hot = "mcp__katailyst2__tool_execute"
    cold = "mcp__posthog__exec"
    entries = {name: types.SimpleNamespace(toolset=toolset) for name, toolset in (
        (hot, "mcp-katailyst2"), (cold, "mcp-posthog"),
        ("read_spillover", "hlt-context"), ("read_file", "file"),
        ("desktop_screenshot", "desktop_ui"),
    )}
    registry = types.ModuleType("tools.registry")
    registry.registry = types.SimpleNamespace(get_entry=entries.get)
    core = types.ModuleType("toolsets")
    core._HERMES_CORE_TOOLS = {"read_file"}
    ns = module.__dict__
    ns.update({
        "dataclass": dataclasses.dataclass, "json": json, "math": math,
        "logger": logging.getLogger(__name__), "CHARS_PER_TOKEN": 4.0,
        "BRIDGE_TOOL_NAMES": bridge_names, "_DIRECT_SURFACE_TOOLSETS": {"desktop_ui", "project"},
        "_MAX_DESCRIBE_NAMES_PER_CALL": 10, "_MAX_DESCRIBE_NAMES_PER_RESPONSE": 3,
        "_MAX_TOOL_DESCRIPTION_CHARS": 4000,
        "tool_error": lambda msg: json.dumps({"error": msg}),
        # Listing text/search ranking is unchanged. Keep this check focused on
        # which real schemas reach the model and which names remain callable.
        "build_catalog_listing_with_form": lambda defs, **kw: (json.dumps(names(defs)), "names"),
        "bridge_tool_schemas": lambda count, **kw: [definition(n) for n in sorted(bridge_names)],
    })
    with patch.dict(sys.modules, {module.__name__: module, "tools.registry": registry, "toolsets": core}):
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), ns)
        config_type = ns["ToolSearchConfig"]
        for raw in (None, True, False, {"always_loaded": hot}, {"always_loaded": None}):
            assert config_type.from_raw(raw).always_loaded == ()
        config = config_type.from_raw({"always_loaded": [hot, " read_spillover ", hot, None, "", 12]})
        assert config.always_loaded == (hot, "read_spillover")
        ns["load_config"] = ns["load_config_readonly"] = lambda: config
        definitions = [definition(n) for n in entries]
        original = json.dumps(definitions)
        assemble = ns["assemble_tool_defs"]
        result = assemble(definitions, config=config, context_length=200000)
        assert result.activated and result.deferred_count == 1
        assert set(names(result.tool_defs)) == {hot, "read_spillover", "read_file", "desktop_screenshot"} | bridge_names
        assert next(td for td in result.tool_defs if td["function"]["name"] == hot) is definitions[0]
        assert json.dumps(definitions) == original
        # Promotion must not expand a restricted child's actual toolset.
        scoped = [definition("read_file"), definition(cold)]
        assert hot not in names(assemble(scoped, config=config).tool_defs)
        assert hot not in ns["scoped_deferrable_names"](scoped)
        # Old bridge calls still work after a restart/configuration change.
        assert hot in ns["scoped_deferrable_names"](definitions)
        described = json.loads(ns["dispatch_tool_describe"]({"names": [hot]}, current_tool_defs=definitions, config=config))
        assert described["tools"][hot]["parameters"] == definitions[0]["function"]["parameters"]
        missing = json.loads(ns["dispatch_tool_describe"]({"names": [hot]}, current_tool_defs=scoped, config=config))
        assert missing["not_found"] == [hot]
        # Off/empty/all-hot configurations retain native behavior and order.
        off = config_type.from_raw({"enabled": "off", "always_loaded": [hot]})
        assert assemble(definitions, config=off).tool_defs == definitions
        only_hot = [definition(hot), definition("read_file")]
        assert not assemble(only_hot, config=config).activated
        assert assemble(only_hot, config=config).tool_defs == only_hot
        assert assemble(definitions, config=config_type.from_raw(None)).deferred_count == 3
        assert hot in names(assemble(definitions).tool_defs)
        # Re-assembly doesn't duplicate native bridge or tool schemas.
        repeated = assemble(definitions + [definition(n) for n in bridge_names], config=config)
        assert names(repeated.tool_defs) == names(result.tool_defs)


def definition(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": "Read the requested capability.", "parameters": {"type": "object", "properties": {"toolRef": {"type": "string"}}, "required": ["toolRef"]}}}


def names(definitions: list[dict]) -> list[str]:
    return [td["function"]["name"] for td in definitions]


if __name__ == "__main__":
    assert_always_loaded_tools(Path(sys.argv[1]))
    print("Native eager-tool assembly, scope and bridge compatibility passed.")
