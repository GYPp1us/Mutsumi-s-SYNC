from __future__ import annotations

from src.mutsumi_sync.config import Config
from src.mutsumi_sync.memory.session import SessionState
from src.mutsumi_sync.memory.store import EventRecord, EventType, MessageStore
from src.mutsumi_sync.memory.window import MessageWindow
from src.mutsumi_sync.message.sender import Peer
from src.mutsumi_sync.pipeline import _archive_if_needed
from src.mutsumi_sync.scheduler import PipelineDeps
from src.mutsumi_sync.tools.registry import ToolRegistry


async def test_ledger_pipeline_never_writes_legacy_message_summary(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "ledger.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="already in ledger",
            status="finalized",
        ))
        deps = PipelineDeps(
            config=Config(),
            registry=ToolRegistry(),
            sender=object(),
            store=store,
            window=MessageWindow(),
            session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="alice"),
            group_key="private:alice",
            conversation_id="private:alice",
        )

        await _archive_if_needed(deps, "long input", ["long output"])

        assert await store.get_summaries("private:alice") == []
    finally:
        await store.close()
