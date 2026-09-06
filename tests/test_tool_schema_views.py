"""Dependency-free regression for callable K2 schemas in Slack/API results."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SERVICE = Path(__file__).resolve().parents[1] / "services" / "agent"
PLUGIN = SERVICE / "hermes_plugins" / "hlt_k2_context"
spec = importlib.util.spec_from_file_location(
    "schema_view_test_plugin", PLUGIN / "__init__.py",
    submodule_search_locations=[str(PLUGIN)],
)
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
views = sys.modules[spec.name + ".tool_result_views"]


def packet():
    """The observed K2 shape, with oversized repeated output contracts."""
    output_schema = {"type": "object", "description": "Readout definition. " * 1500}
    return {
        "detailLevel": "schema",
        "resourceHint": "katailyst://registry/capabilities#tool:nm-analytics-readout",
        "tool": {
            "kind": "registry_tool", "schemaVersion": "capability-packet.v2",
            "toolRef": "tool:nm-analytics-readout", "selectedAction": "readout",
            "selection": {"doNotUseWhen": "Do not invent missing measurements."},
            "route": {"kind": "tool.execute", "dispatchable": True},
            "readiness": {"state": "ready"},
            "actions": [{
                "action": "readout", "description": "Read the requested window.",
                "argsSchema": {
                    "type": "object", "required": ["action", "days"],
                    "properties": {"days": {"enum": [7, 28, 90], "type": "integer"},
                                   "keys": {"type": "string", "maxLength": 600},
                                   "action": {"const": "readout"}},
                    "additionalProperties": False,
                },
                "riskTier": "low", "idempotency": "read",
                "prerequisites": {"credential": "caller binding"},
                "example": {"action": "readout", "days": 7},
                "outputSchemaRef": "schema:nm_analytics_readout.v1",
                "outputSchema": output_schema,
            }],
            "contracts": {"inputSchema": None, "outputSchema": output_schema},
        },
    }


class ToolSchemaViewsTest(unittest.TestCase):
    def test_nested_live_shape_exposes_complete_inputs_inline(self):
        original = packet()
        encoded = json.dumps({"result": json.dumps(original)})
        before = json.dumps(original, sort_keys=True)
        preview = views.tool_schema_preview(encoded)
        self.assertIsNotNone(preview)
        self.assertLessEqual(len(preview), 8000)
        decoded = json.loads(preview.split("\n", 1)[1])
        action = decoded["tool"]["actions"][0]
        self.assertEqual(action["argsSchema"], original["tool"]["actions"][0]["argsSchema"])
        self.assertEqual(action["prerequisites"], {"credential": "caller binding"})
        self.assertEqual(action["example"], {"action": "readout", "days": 7})
        self.assertNotIn("outputSchema", action)
        self.assertNotIn("outputSchema", decoded["tool"]["contracts"])
        self.assertEqual(before, json.dumps(original, sort_keys=True))

    def test_known_envelopes_and_transport_pointer(self):
        value = packet()
        transport = {"contractVersion": "tool_execute_transport.v1", "mode": "model_visible_text", "modelVisibleText": "content[0].text"}
        compact = {"structuredContent": {"transportCompaction": transport}, "content": [{"type": "text", "text": json.dumps({"output": value})}]}
        for envelope in (value, {"result": json.dumps(value)}, {"output": value},
                         {"structuredContent": value}, compact, {"result": json.dumps(compact)}):
            with self.subTest(envelope=list(envelope)):
                self.assertEqual(json.loads(views.tool_schema_view(envelope))["tool"]["toolRef"], "tool:nm-analytics-readout")
        transport["modelVisibleText"] = "__import__('os').environ"
        self.assertIsNone(views.tool_schema_view(compact))

    def test_canonical_schema_retains_definitions_and_similarly_named_inputs(self):
        schema = {"$defs": {"rule": {"type": "string"}}, "type": "object", "properties": {"outputSchema": {"$ref": "#/$defs/rule"}}}
        value = {"detailLevel": "schema", "tool": {"name": "tool_execute", "inputSchema": schema, "outputSchema": {"description": "big" * 10000}, "requiredScope": "tool.execute"}}
        projected = json.loads(views.tool_schema_view(value))["tool"]
        self.assertEqual(projected["inputSchema"], schema)
        self.assertEqual(projected["requiredScope"], "tool.execute")
        value["tool"]["inputSchema"] = False
        self.assertIs(json.loads(views.tool_schema_view(value))["tool"]["inputSchema"], False)

    def test_non_schema_and_failed_results_are_not_recast_as_callable(self):
        for value in ("not JSON", {"body_md": "article"}, {"output": {"readouts": []}},
                      {"isError": True, "output": packet()}, {"status": "failed", "output": packet()},
                      {"detailLevel": "schema", "tool": {"kind": "registry_tool", "actions": [None]}},
                      "x" * (views.MAX_SCHEMA_SOURCE_BYTES + 1)):
            with self.subTest(kind=type(value).__name__):
                self.assertIsNone(views.tool_schema_view(value))

    def test_nonready_state_and_every_action_survive(self):
        value = packet()
        value["tool"]["readiness"] = {"state": "approval_required", "nextAction": "ask"}
        other = {"action": "stage", "argsSchema": {"type": "object"}, "riskTier": "high"}
        value["tool"]["actions"].append(other)
        projected = json.loads(views.tool_schema_view(value))["tool"]
        self.assertEqual(projected["readiness"], value["tool"]["readiness"])
        self.assertEqual(projected["actions"][1], other)

    def test_unresolved_input_contract_is_not_labelled_complete(self):
        value = packet()
        value["tool"]["actions"][0]["argsSchema"] = None
        self.assertIsNone(views.tool_schema_view(value))

    def test_existing_body_and_raw_views_still_recover_original_results(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HERMES_HOME": home}):
            session = "hook:k2:existing-views"
            root = Path(home) / "cache" / "spillover"
            root.mkdir(parents=True)
            path = root / (plugin._spillover_session_prefix(session) + "_body.txt")
            body = "Nurse evidence 🌱\n" * 100
            encoded = json.dumps({"output": {"body_md": body}})
            path.write_text(encoded)
            raw = json.loads(plugin._read_spillover({"handle": str(path), "limit": 12000}, session_id=session))
            decoded = json.loads(plugin._read_spillover({"handle": str(path), "view": "body", "limit": 12000}, session_id=session))
            self.assertEqual(raw["content"], encoded)
            self.assertEqual(decoded["content"], body)
            self.assertFalse(decoded["hasMore"])
            schema = json.loads(plugin._read_spillover({"handle": str(path), "view": "schema"}, session_id=session))
            self.assertIn("no tool schema", schema["error"])

    def test_oversized_input_is_paged_never_mislabelled_complete_inline(self):
        value = packet()
        schema = value["tool"]["actions"][0]["argsSchema"]
        schema["$defs"] = {"large": {"description": "Evidence 🌱\n" * 1500}}
        self.assertIsNone(views.tool_schema_preview(json.dumps(value)))
        for session in ("hook:k2:schema-case", "slack:D-example:thread:1"):
            with self.subTest(session=session), tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HERMES_HOME": home}):
                root = Path(home) / "cache" / "spillover"
                root.mkdir(parents=True)
                filename = hashlib.sha256(session.encode()).hexdigest()[:20] + "_call.txt"
                path = root / filename
                source = json.dumps({"result": json.dumps(value)}).encode()
                path.write_bytes(source)
                offset, chunks = 0, []
                for _ in range(50):
                    page = json.loads(plugin._read_spillover({"handle": filename, "view": "schema", "offset": offset, "limit": 997}, session_id=session))
                    self.assertNotIn("error", page)
                    self.assertLessEqual(page["returnedBytes"], 1000)
                    chunks.append(page["content"])
                    if not page["hasMore"]:
                        break
                    offset = page["nextOffset"]
                else:
                    self.fail("schema pagination failed to terminate")
                self.assertEqual(json.loads("".join(chunks))["tool"]["actions"][0]["argsSchema"], schema)
                self.assertEqual(path.read_bytes(), source)
                denied = json.loads(plugin._read_spillover({"handle": filename, "view": "schema"}, session_id="other"))
                self.assertIn("does not belong", denied["error"])
                outside = Path(home) / "outside.txt"
                outside.write_text(json.dumps(value))
                path.unlink()
                path.symlink_to(outside)
                self.assertIn("outside the spillover store", json.loads(plugin._read_spillover({"handle": filename, "view": "schema"}, session_id=session))["error"])

    def test_reader_registration_and_image_share_implementation(self):
        registered = {}
        class Context:
            def register_tool(self, **kwargs):
                registered.update(kwargs)
            def register_hook(self, *_args):
                pass
        plugin.register(Context())
        self.assertIn("schema", registered["schema"]["parameters"]["properties"]["view"]["enum"])
        self.assertIn("view:'schema'", plugin.HOSTED_K2_CONTEXT)
        docker = (SERVICE / "Dockerfile").read_text()
        self.assertIn("COPY hermes_plugins/hlt_k2_context/tool_result_views.py /app/hlt_tool_result_views.py", docker)


if __name__ == "__main__":
    unittest.main()
