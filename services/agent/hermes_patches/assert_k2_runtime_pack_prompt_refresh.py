"""Build-time proof that managed K2 pack changes refresh Hermes sessions."""

from __future__ import annotations

import ast
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace


def assert_prompt_refresh_contract(hermes_root: Path) -> None:
    source_path = hermes_root / "agent" / "conversation_loop.py"
    source = source_path.read_text(encoding="utf-8")
    compile(source, str(source_path), "exec")
    assert "def active_k2_pack_source()" in source
    assert 'source_prefix = "<!-- source: katailyst2 agents.runtime_pack "' in source
    assert "if k2_pack_source and k2_pack_source not in prompt:" in source

    prompt_source_path = hermes_root / "agent" / "system_prompt.py"
    prompt_source = prompt_source_path.read_text(encoding="utf-8")
    compile(prompt_source, str(prompt_source_path), "exec")
    assert "def _k2_runtime_pack_read_lock(" in prompt_source
    assert 'lock_path = Path(home) / ".hlt-k2-runtime-pack.lock"' in prompt_source
    assert "fcntl.LOCK_SH" in prompt_source
    assert "with _k2_runtime_pack_read_lock(agent):" in prompt_source

    tree = ast.parse(source, filename=str(source_path))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_stored_prompt_matches_runtime"
    )
    namespace: dict[str, object] = {}
    exec(  # noqa: S102 - executes one AST node from the pinned local source
        compile(
            ast.Module(body=[function], type_ignores=[]),
            str(source_path),
            "exec",
        ),
        namespace,
    )
    prompt_matches = namespace["_stored_prompt_matches_runtime"]

    with tempfile.TemporaryDirectory() as raw_home:
        home = Path(raw_home)
        agent = SimpleNamespace(
            model="test-model",
            provider="test-provider",
            platform="slack",
            _session_db=SimpleNamespace(db_path=home / "state.db"),
        )
        agent_package = ModuleType("agent")
        agent_package.__path__ = []
        system_prompt_module = ModuleType("agent.system_prompt")
        system_prompt_module._agent_home = lambda candidate: Path(
            candidate._session_db.db_path
        ).parent
        previous_agent = sys.modules.get("agent")
        previous_system_prompt = sys.modules.get("agent.system_prompt")
        sys.modules["agent"] = agent_package
        sys.modules["agent.system_prompt"] = system_prompt_module
        marker_v1 = (
            "<!-- source: katailyst2 agents.runtime_pack agent:cleo@1 -->"
        )
        marker_v2 = (
            "<!-- source: katailyst2 agents.runtime_pack agent:cleo@2 -->"
        )

        # The reviewed bundled fallback has no K2 source epoch and therefore
        # does not manufacture a perpetual cache miss.
        (home / "SOUL.md").write_text("# Cleo\n", encoding="utf-8")
        assert prompt_matches(agent, "cached fallback") is True

        # Installing the active pack invalidates an existing fallback prompt
        # exactly once; the rebuilt prompt is reusable until the pack version
        # changes again.
        (home / "SOUL.md").write_text(f"{marker_v1}\n# Cleo\n", encoding="utf-8")
        assert prompt_matches(agent, "cached fallback") is False
        assert prompt_matches(agent, f"{marker_v1}\nnew prompt") is True
        (home / "SOUL.md").write_text(f"{marker_v2}\n# Cleo\n", encoding="utf-8")
        assert prompt_matches(agent, f"{marker_v1}\nold prompt") is False
        if previous_agent is None:
            sys.modules.pop("agent", None)
        else:
            sys.modules["agent"] = previous_agent
        if previous_system_prompt is None:
            sys.modules.pop("agent.system_prompt", None)
        else:
            sys.modules["agent.system_prompt"] = previous_system_prompt


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(
            "usage: assert_k2_runtime_pack_prompt_refresh.py HERMES_ROOT"
        )
    assert_prompt_refresh_contract(Path(sys.argv[1]))
