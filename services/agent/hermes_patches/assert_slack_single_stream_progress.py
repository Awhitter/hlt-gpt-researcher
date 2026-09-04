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

    # Importing the Slack adapter before the image's optional SDK extras are
    # installed would make this assertion depend on the build order. Source
    # assertions pin its two presentation capabilities; the executable fake
    # below proves the shared stream consumer behavior.
    slack_source = (
        root / "plugins" / "platforms" / "slack" / "adapter.py"
    ).read_text(encoding="utf-8")
    run_source = (root / "gateway" / "run.py").read_text(encoding="utf-8")
    base_source = (root / "gateway" / "platforms" / "base.py").read_text(
        encoding="utf-8"
    )
    slash_source = (root / "gateway" / "slash_commands.py").read_text(
        encoding="utf-8"
    )
    approval_source = (root / "tools" / "approval.py").read_text(
        encoding="utf-8"
    )
    stream_consumer_source = (
        root / "gateway" / "stream_consumer.py"
    ).read_text(encoding="utf-8")
    assert "draft_stream_is_message = True" in slack_source
    assert 'initial_stream_ack = "On it' in slack_source
    assert 'self._app.event("agent_session_stopped")' in slack_source
    assert "gateway_run_generation" in slack_source
    assert "agents.sessions.setStatus" in slack_source
    assert "_finalized_streams" in slack_source
    assert "_uncertain_stream_starts" in slack_source
    assert "require_completion=True" in slack_source
    assert "_pending_agent_stop_tasks" in slack_source
    assert "_confirmed_agent_stop_workers" in slack_source
    assert "append_attempted = False" in slack_source
    assert "if append_attempted:" in slack_source
    assert "thread_active = [" in slack_source
    assert "if stopped_ts and thread_active and not candidates:" in slack_source
    assert "if finalized_match:" in slack_source
    assert "_slack_safe_stream_failure" in run_source
    assert "HLT_MANAGED_MODEL_ROUTE" in run_source
    assert "_register_managed_turn_control" in run_source
    assert "_prime_managed_slack_turn_stream" in run_source
    assert "_finish_managed_slack_admission_failure" in run_source
    assert "_request_and_confirm_managed_turn_stop" in run_source
    assert "managed_turn_control: Optional[Dict[str, Any]] = None" in run_source
    assert "if not _managed_slack_stream:" in run_source
    assert 'model = "gpt-5.6-sol"' in run_source
    assert 'provider = "xai-oauth"' in run_source
    assert 'model = "grok-4.6"' in run_source
    assert "def _managed_fallback_chain()" in run_source
    assert "_publish_managed_slack_stream_progress" in run_source
    assert run_source.count("await self._send_slack_lifecycle_notice(") == 5
    assert "_should_send_trailing_runtime_footer" in run_source
    assert "_managed_slack_status_progress_message" in run_source
    assert "HLT_MANAGED_MODEL_ROUTE" in slash_source
    assert "require_completion: bool = False" in base_source
    assert "return cancellation_confirmed" in base_source
    assert "_EXTERNAL_WRITE_PATTERNS" in approval_source
    assert "send data to an external service (curl)" in approval_source
    assert "def _managed_execute_code_effect" in approval_source
    assert 'pattern_key = f"execute_code:{effect_kind}"' in approval_source
    assert (
        "if self._already_sent and self.has_delivered_text(final_text):"
        in stream_consumer_source
    )
    assert "self._delivery_ambiguous = True" in stream_consumer_source
    fresh_final_start = stream_consumer_source.index("async def _try_fresh_final")
    fresh_final_end = stream_consumer_source.index(
        "async def _suppress_silence_marker", fresh_final_start
    )
    assert (
        "self._record_turn_final_payload(text)"
        in stream_consumer_source[fresh_final_start:fresh_final_end]
    )
    send_or_edit_start = stream_consumer_source.index("async def _send_or_edit")
    send_or_edit_source = stream_consumer_source[send_or_edit_start:]
    optimistic_record_at = send_or_edit_source.index(
        "self._record_turn_final_payload(text)"
    )
    optimistic_rollback_at = send_or_edit_source.index(
        "self._delivered_final_text = None", optimistic_record_at
    )
    assert optimistic_record_at < optimistic_rollback_at
    # The sole acknowledgement stream opens immediately after admission,
    # before even session/history lookup or attachment preprocessing. The
    # exact executor worker is then published so Stop can wait for real work,
    # not merely its asyncio wrapper.
    handle_start = run_source.index("async def _handle_message_with_agent")
    prime_at = run_source.index(
        "consumer = await self._prime_managed_slack_turn_stream(",
        handle_start,
    )
    session_lookup_at = run_source.index("# Get or create session", prime_at)
    assert prime_at < session_lookup_at
    worker_stop_at = slack_source.index("worker_stopped = await confirm_worker_stop(")
    wrapper_stop_at = slack_source.index(
        "cancellation_completed = await self.cancel_session_processing(",
        worker_stop_at,
    )
    invalidate_at = slack_source.index(
        "await runner._interrupt_and_clear_session(", wrapper_stop_at
    )
    assert worker_stop_at < wrapper_stop_at < invalidate_at

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
