"""Pin verified admission before typing and shared Slack dispatch."""
from __future__ import annotations
import ast
import sys
from pathlib import Path

def method(path, name):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return ast.get_source_segment(source, node)

def assert_pretyping_contract(root):
    base = root / "gateway/platforms/base.py"
    admission = method(base, "handle_message")
    assert admission.index('"pre_gateway_dispatch"') < admission.index("coerce_plaintext_gateway_command(event)")
    assert admission.index('"pre_gateway_dispatch"') < admission.rindex("self._start_session_processing(event, session_key)")
    assert '"_hermes_pre_gateway_dispatch_done"' in admission
    assert '"channel_context" in result' in admission
    assert "dataclasses.replace(event, **changes)" in admission
    hook = method(root / "gateway/run_inbound.py", "_hm_pre_gateway_dispatch_hook")
    assert '"_hermes_pre_gateway_dispatch_done"' in hook
    assert '"pre_gateway_dispatch"' in (root / "hermes_cli/plugins.py").read_text()
    slack = root / "plugins/platforms/slack/adapter.py"
    human = method(slack, "_drop_bot_sender")
    assert 'event["_hermes_sender_is_bot"] = True' in human
    assert "fail_closed=True" in human
    assert 'event["_hermes_verified_human_app_relay"] = True' in human
    hydrate = method(slack, "_hydrate_thread_context")
    assert 'after_ts="" if bare_agent_transfer else self._get_thread_watermark' in hydrate

if __name__ == "__main__":
    assert_pretyping_contract(Path(sys.argv[1]))
