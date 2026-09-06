"""Readable K2 call contracts; full provider results remain in native storage.

The image also copies this dependency-free module to /app so the upstream
storage overlay and the session-bound plugin reader use the same projection.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

MAX_SCHEMA_SOURCE_BYTES = 1_048_576
MAX_SCHEMA_PREVIEW_CHARS = 8_000


def tool_schema_view(value: Any) -> str | None:
    """Decode known transport envelopes, retaining complete input contracts.

    Only authored tool.describe payloads qualify. Output schemas are not call
    arguments and stay in the saved original; never recursively prune schema
    properties, definitions, examples, effects, or readiness requirements.
    """
    for _ in range(8):
        if isinstance(value, str):
            if len(value) > MAX_SCHEMA_SOURCE_BYTES:
                return None
            try:
                value = json.loads(value)
            except (ValueError, RecursionError):
                return None
        if not isinstance(value, Mapping) or value.get("isError") is True:
            return None
        if value.get("status") in ("error", "failed"):
            return None

        tool = value.get("tool")
        if value.get("detailLevel") == "schema" and isinstance(tool, Mapping):
            graph_tool = (
                tool.get("schemaVersion") == "capability-packet.v2"
                and tool.get("kind") == "registry_tool"
                and isinstance(tool.get("toolRef"), str)
                and isinstance(tool.get("actions"), list)
                and bool(tool["actions"])
                and all(
                    isinstance(action, Mapping)
                    and isinstance(action.get("argsSchema"), (Mapping, bool))
                    for action in tool["actions"]
                )
            )
            canonical_tool = (
                isinstance(tool.get("name"), str)
                and isinstance(tool.get("inputSchema"), (Mapping, bool))
            )
            if not graph_tool and not canonical_tool:
                return None
            projected = {key: item for key, item in tool.items() if key != "outputSchema"}
            if graph_tool:
                projected["actions"] = [
                    {key: item for key, item in action.items() if key != "outputSchema"}
                    for action in tool["actions"]
                ]
                contracts = tool.get("contracts")
                if isinstance(contracts, Mapping):
                    projected["contracts"] = {
                        key: item for key, item in contracts.items() if key != "outputSchema"
                    }
            return json.dumps({
                "detailLevel": "schema",
                "resourceHint": value.get("resourceHint"),
                "tool": projected,
                "savedDetail": "Output schemas remain in the original saved result (view:raw). Input schemas and action requirements below are complete.",
            }, ensure_ascii=False, indent=2)

        structured = value.get("structuredContent")
        transport = (
            structured.get("transportCompaction")
            if isinstance(structured, Mapping)
            else value.get("transportCompaction")
        )
        if isinstance(transport, Mapping) and transport.get("mode") == "model_visible_text":
            if (
                transport.get("contractVersion") != "tool_execute_transport.v1"
                or transport.get("modelVisibleText") != "content[0].text"
            ):
                return None
            content = value.get("content")
            first = content[0] if isinstance(content, list) and content else None
            value = (
                first.get("text")
                if isinstance(first, Mapping) and first.get("type") == "text"
                else value.get("result")
            )
        elif isinstance(value.get("output"), (Mapping, str)):
            value = value["output"]
        elif isinstance(structured, Mapping):
            value = structured
        elif isinstance(value.get("result"), (Mapping, str)):
            value = value["result"]
        else:
            content = value.get("content")
            first = content[0] if isinstance(content, list) and content else None
            value = (
                first.get("text")
                if isinstance(first, Mapping) and first.get("type") == "text"
                else None
            )
    return None


def tool_schema_preview(content: str) -> str | None:
    """Use a complete small call contract instead of an arbitrary prefix."""
    schema = tool_schema_view(content)
    if schema is None:
        return None
    preview = "Complete call/input schema from the saved result:\n" + schema
    return preview if len(preview) <= MAX_SCHEMA_PREVIEW_CHARS else None
