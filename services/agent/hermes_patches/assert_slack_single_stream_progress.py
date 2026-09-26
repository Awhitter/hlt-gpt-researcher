#!/usr/bin/env python3
"""Build-time proof for Cleo's one-stream Slack teammate lifecycle."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path


def main(root: Path) -> None:
    sys.path.insert(0, str(root))

    from gateway.config import Platform
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

    import inspect
    from gateway.run import GatewayRunner
    from gateway.run_managed_slack import _managed_fallback_chain
    from gateway.run_turn_runner import TurnRunner
    slack_source = (root / "plugins/platforms/slack/adapter.py").read_text(encoding="utf-8")
    assert "draft_stream_is_message = True" in slack_source
    assert 'initial_stream_ack = "On it' in slack_source
    assert '("agent_session_stopped", self._handle_agent_session_stopped)' in slack_source
    for marker in ("_finalized_streams", "_uncertain_stream_starts", "_pending_agent_stop_tasks",
                   "_confirmed_agent_stop_workers", "require_completion=True", "gateway_run_generation"):
        assert marker in slack_source, marker
    worker_stop = slack_source.index("worker_stopped = await confirm_worker_stop(")
    wrapper_stop = slack_source.index("cancellation_completed = await self.cancel_session_processing(", worker_stop)
    invalidation = slack_source.index("await runner._interrupt_and_clear_session(", wrapper_stop)
    assert worker_stop < wrapper_stop < invalidation
    handle = inspect.getsource(GatewayRunner._handle_message_with_agent)
    assert handle.index("await self._prime_managed_slack_turn_stream(") < handle.index("await self._handle_message_with_agent_admitted(")
    worker = inspect.getsource(GatewayRunner._run_agent_inner)
    assert 'managed_turn_control["worker_done"] = worker.worker_done' in worker
    assert 'managed_turn_control["executor_task"] = worker.executor_task' in worker
    stop = inspect.getsource(GatewayRunner._request_and_confirm_managed_turn_stop)
    assert "worker_done.is_set()" in stop and "request_hard_interrupt" in stop
    assert "_slack_safe_stream_failure" in inspect.getsource(TurnRunner._finish_stream_consumer)
    assert _managed_fallback_chain() == [{"provider": "openrouter", "model": "openai/gpt-6-astra"}]

    class RecordingStreamAdapter(BasePlatformAdapter):
        draft_stream_is_message = True
        initial_stream_ack = "On it — checking Nursing Mastery now."
        MAX_MESSAGE_LENGTH = 39_000

        def __init__(self) -> None:
            # The focused consumer proof does not connect to a platform. Base
            # initialization provides the ordinary adapter bookkeeping only.
            super().__init__(config=None, platform=Platform.SLACK)  # type: ignore[arg-type]
            self.draft_frames: list[tuple[int, str]] = []
            self.final_sends: list[str] = []

        async def connect(self, *, is_reconnect: bool = False) -> bool:
            return True

        async def disconnect(self) -> None:
            return None

        async def send(
            self,
            chat_id: str,
            content: str,
            reply_to: str | None = None,
            metadata: dict | None = None,
        ) -> SendResult:
            self.final_sends.append(content)
            return SendResult(success=True, message_id="stream-final")

        async def get_chat_info(self, chat_id: str) -> dict:
            return {"name": "agent-logs", "type": "channel"}

        def supports_draft_streaming(
            self,
            chat_type: str | None = None,
            metadata: dict | None = None,
            chat_id: str | None = None,
        ) -> bool:
            return True

        async def send_draft(
            self,
            chat_id: str,
            draft_id: int,
            content: str,
            metadata: dict | None = None,
        ) -> SendResult:
            self.draft_frames.append((draft_id, content))
            return SendResult(success=True, message_id="stream-live")

    def recordless_consumer(
        adapter: RecordingStreamAdapter,
        *,
        visible_text: str = "",
        already_sent: bool,
        delivery_ambiguous: bool = False,
    ) -> GatewayStreamConsumer:
        consumer = GatewayStreamConsumer(
            adapter=adapter,
            chat_id="C_AGENT_LOGS",
            config=StreamConsumerConfig(cursor=""),
            metadata={"thread_id": "1787140000.000100"},
            initial_reply_to_id="1787140000.000100",
        )
        consumer._final_response_sent = True
        consumer._final_content_delivered = True
        consumer._delivered_final_text = None
        consumer._turn_split_delivery = False
        consumer._delivery_ambiguous = delivery_ambiguous
        consumer._already_sent = already_sent
        consumer._last_sent_text = visible_text
        consumer._delivered_commentary_texts = []
        consumer._delivered_segment_texts = []
        return consumer

    async def verify() -> None:
        adapter = RecordingStreamAdapter()
        complete = "The content package is ready for staged review."
        partial = "The content package is ready"
        assert (
            recordless_consumer(
                adapter,
                visible_text=partial,
                already_sent=True,
            ).delivered_final_matches(complete)
            is False
        )
        assert (
            recordless_consumer(
                adapter,
                visible_text=complete,
                already_sent=True,
            ).delivered_final_matches(complete)
            is True
        )
        assert (
            recordless_consumer(
                adapter,
                visible_text=complete,
                already_sent=False,
            ).delivered_final_matches(complete)
            is False
        )
        assert (
            recordless_consumer(
                adapter,
                already_sent=True,
                delivery_ambiguous=True,
            ).delivered_final_matches(complete)
            is None
        )
        consumer = GatewayStreamConsumer(
            adapter=adapter,
            chat_id="C_AGENT_LOGS",
            config=StreamConsumerConfig(
                transport="auto",
                edit_interval=0.01,
                buffer_threshold=1,
                cursor="",
            ),
            metadata={"thread_id": "1787140000.000100"},
            initial_reply_to_id="1787140000.000100",
        )
        # Provider selection is deliberately still blocked while the admitted
        # Slack turn gets its first visible chunk. The threshold matches the
        # human-facing two-second contract without performing model inference.
        provider = asyncio.create_task(asyncio.sleep(3))
        started = time.monotonic()
        await consumer.prime()
        assert time.monotonic() - started < 2
        assert not provider.done()
        provider.cancel()
        try:
            await provider
        except asyncio.CancelledError:
            pass

        task = asyncio.create_task(consumer.run())
        consumer.on_commentary("Checking the current Drive assets and site.")
        await asyncio.sleep(0.05)
        consumer.on_delta("The package is ready for staged review.")
        consumer.on_segment_break()
        consumer.finish("The package is ready for staged review.")
        await asyncio.wait_for(task, timeout=2)

        assert adapter.draft_frames
        draft_ids = {draft_id for draft_id, _ in adapter.draft_frames}
        assert len(draft_ids) == 1, adapter.draft_frames
        assert adapter.draft_frames[0][1] == (
            "On it — checking Nursing Mastery now.\n\n"
        )
        assert any(
            "Checking the current Drive assets and site." in content
            for _, content in adapter.draft_frames
        )
        assert adapter.final_sends == [
            "On it — checking Nursing Mastery now.\n\n"
            "Checking the current Drive assets and site.\n\n"
            "The package is ready for staged review."
        ]

    asyncio.run(verify())
    print("Hermes Slack one-stream progress contract OK")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: assert_slack_single_stream_progress.py HERMES_ROOT")
    main(Path(sys.argv[1]).resolve())
