"""Pin the Hermes admission seam that prevents suppressed Slack typing flicker."""

from __future__ import annotations

import sys
from pathlib import Path


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


def assert_pretyping_contract(hermes_root: Path) -> None:
    base_source = (hermes_root / "gateway/platforms/base.py").read_text(
        encoding="utf-8"
    )
    runner_source = (hermes_root / "gateway/run.py").read_text(encoding="utf-8")
    plugin_source = (hermes_root / "hermes_cli/plugins.py").read_text(encoding="utf-8")
    slack_source = (hermes_root / "plugins/platforms/slack/adapter.py").read_text(
        encoding="utf-8"
    )

    handle_message = _between(
        base_source,
        "    async def handle_message(self, event: MessageEvent)",
        "    @staticmethod\n    def _get_human_delay",
    )
    background = _between(
        base_source,
        "    async def _process_message_background",
        "    def _cleanup_finished_session_task",
    )
    runner_hook = _between(
        runner_source,
        "        # Fire pre_gateway_dispatch plugin hook",
        "        if is_internal:",
    )

    assert handle_message.index('"pre_gateway_dispatch"') < handle_message.index(
        "coerce_plaintext_gateway_command(event)"
    )
    assert handle_message.index('"pre_gateway_dispatch"') < handle_message.rindex(
        "self._start_session_processing(event, session_key)"
    )
    assert background.index("self._keep_typing(") < background.index(
        "self._message_handler(event)"
    )
    assert '"_hermes_pre_gateway_dispatch_done"' in handle_message
    assert '"_hermes_pre_gateway_dispatch_done"' in runner_hook
    assert '"pre_gateway_dispatch"' in plugin_source
    assert 'event["_hermes_sender_is_bot"] = True' in slack_source


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: assert_pre_gateway_dispatch.py HERMES_ROOT")
    assert_pretyping_contract(Path(sys.argv[1]))
