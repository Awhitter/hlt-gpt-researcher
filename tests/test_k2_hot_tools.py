"""Focused stdlib checks: K2 hot path and the observed persistent-skill drift."""
from __future__ import annotations

import ast
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SERVICE = Path(__file__).resolve().parents[1] / "services" / "agent"


def load(name, path, package=False):
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(path.parent)] if package else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


grounding = load("hot_tools_grounding", SERVICE / "grounding.py")
plugin = load("hot_tools_plugin", SERVICE / "hermes_plugins" / "hlt_k2_context" / "__init__.py", True)
native = load("hot_tools_native", SERVICE / "hermes_patches" / "assert_always_loaded_tools.py")
# build_config is pure; this check does not call render() or YAML IO.
with patch.dict(sys.modules, {"yaml": types.ModuleType("yaml")}):
    config = load("hot_tools_config", SERVICE / "render_config.py")


class HotToolsTest(unittest.TestCase):
    def test_exact_pinned_native_assembly_and_bridge_scope(self):
        root = os.environ.get("HERMES_TEST_ROOT")
        self.assertTrue(root, "Set HERMES_TEST_ROOT to the patched pinned source fixture")
        native.assert_always_loaded_tools(Path(root))

    def test_five_tools_visible_in_render_config_without_disabling_catalog(self):
        value = config.build_config({"KATAILYST2_MCP_URL": "https://k2.example/mcp", "KATAILYST2_MCP_TOKEN": "fixture"})
        hot = value["tools"]["tool_search"]["always_loaded"]
        self.assertEqual(len(hot), 5)
        self.assertEqual(set(hot), {"mcp__katailyst2__" + verb for verb in ("registry_get", "tool_search", "tool_describe", "tool_execute")} | {"read_spillover"})
        self.assertEqual(value["tools"]["tool_search"]["enabled"], "auto")
        self.assertIn("mcp-katailyst2", value["platform_toolsets"]["slack"])

    def test_same_agreement_slack_hosted_and_discovery_outage(self):
        with patch.object(plugin, "draw_mission_context", return_value={"context": "Useful evidence"}) as draw:
            slack = plugin._pre_llm_call(user_message="Read Nursing Mastery funnel counts", platform="slack", session_id="slack-fixture")
            hosted = plugin._pre_llm_call(user_message="Read Nursing Mastery funnel counts", platform="api_server", session_id="hook:k2:fixture")
            self.assertEqual(draw.call_count, 1)
            for result in (slack, hosted):
                self.assertEqual(result["context"].count(plugin.K2_TOOL_CONTEXT), 1)
                self.assertIn("tool:* capability ref is NOT a native", result["context"])
            draw.return_value = {}
            result = plugin._pre_llm_call(user_message="Read Nursing Mastery funnel counts", platform="slack")
            self.assertEqual(result["context"], plugin.K2_TOOL_CONTEXT)
        self.assertIsNone(plugin._pre_llm_call(user_message="thanks"))

    def test_runtime_image_applies_and_checks_overlay(self):
        docker = (SERVICE / "Dockerfile").read_text()
        self.assertIn("apply --check /tmp/hermes-patches/always_loaded_tools.patch", docker)
        self.assertIn("assert_always_loaded_tools.py /opt/hermes", docker)
        self.assertIn("analytics_skill_refreshed", (SERVICE / "grounding.py").read_text())

    def test_existing_boot_presentation_contracts(self):
        # Execute just the three affected dependency-free boot contracts, not
        # the monolithic pytest file or any of its unrelated integration cases.
        path = SERVICE.parents[1] / "tests" / "test_agent_boot.py"
        wanted = {"test_slack_gets_its_own_prompt_guidance", "test_cleo_fast_paths_named_k2_sources_and_governed_funnel_readout", "test_cleo_keyed_funnel_pulse_stays_inline_and_keeps_the_native_table"}
        nodes = [node for node in ast.parse(path.read_text()).body if isinstance(node, ast.FunctionDef) and node.name in wanted]
        self.assertEqual(len(nodes), len(wanted))
        namespace = {"render_config": config, "SERVICE_DIR": SERVICE, "FULL_ENV": {}, "_load_k2_plugin": lambda: plugin}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
        for name in wanted:
            with self.subTest(contract=name):
                namespace[name]()

    def test_skill_refresh_keeps_custom_analysis_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / "skills" / "nursing-mastery-posthog"
            (root / "references").mkdir(parents=True)
            skill = root / "SKILL.md"
            original = ('---\nname: nursing-mastery-posthog\ndescription: "Use when counting NM landings, bounce, clicks, or conversions in PostHog. Chart splits; never call a pageview a click."\n---\n\n## Prerequisites\n\nMy custom click query.\none action. No process narration. Slack: code-block table, not markdown.\n')
            skill.write_text(original)
            reference = root / "references/three-authority-owner-readout.md"
            reference.write_text('Use with `references/recruiting-funnel-readback.md`. This is the owner\nSlack has\nno tables — code block plus a 7d stage-bar from the same counts.\nRetain source IDs.')
            self.assertEqual(len(grounding.refresh_analytics_skill(home)), 2)
            self.assertIn("My custom click query.", skill.read_text())
            self.assertIn("tool:nm-analytics-readout", skill.read_text())
            self.assertNotIn("Slack has\nno tables", reference.read_text())
            self.assertIn("Retain source IDs.", reference.read_text())
            after = skill.read_bytes(), reference.read_bytes()
            self.assertEqual(grounding.refresh_analytics_skill(home), [])
            self.assertEqual(after, (skill.read_bytes(), reference.read_bytes()))

    def test_absent_or_unrelated_skills_and_souls_are_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            soul = home / "SOUL.md"
            soul.write_text("Her own voice and judgment.")
            self.assertEqual(grounding.refresh_analytics_skill(home), [])
            skill = home / "skills/nursing-mastery-posthog/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("An entirely revised custom skill.\n## Prerequisites\nMy own procedure.")
            self.assertEqual(grounding.refresh_analytics_skill(home), [])
            self.assertEqual(skill.read_text(), "An entirely revised custom skill.\n## Prerequisites\nMy own procedure.")
            self.assertEqual(soul.read_text(), "Her own voice and judgment.")


if __name__ == "__main__":
    unittest.main()
