#!/usr/bin/env python3
"""Build-time proof for Cleo's one-stream Slack teammate lifecycle."""

from __future__ import annotations

import asyncio
import sys
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
    assert "draft_stream_is_message = True" in slack_source
    assert 'initial_stream_ack = "On it' in slack_source

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

    async def verify() -> None:
        adapter = RecordingStreamAdapter()
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
        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.05)
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
