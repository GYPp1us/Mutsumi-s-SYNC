import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from src.mutsumi_sync.memory.store import EventRecord
from src.mutsumi_sync.message.sender import Peer
from src.mutsumi_sync.pipeline import LLMResult
from src.mutsumi_sync.prototype.adapters import ConsoleSender, PrototypePrompts
from src.mutsumi_sync.prototype.runtime import PrototypeScheduler, RuntimeSettings
from src.mutsumi_sync.prototype.storage import PrototypeStore
from src.mutsumi_sync.tools.registry import Tool, ToolRegistry


PEER = Peer(chat_type=1, peer_uid="123")
KEY = "private:123"
PROMPTS = PrototypePrompts(chat="聊天规则", worker="后台规则", wait="收到，稍等一下。", error="这次没能接上。", persona="角色")


def reply(*messages: str) -> LLMResult:
    return LLMResult(content='[TO_SELF][/TO_SELF][TO_USER]' + json.dumps({"messages": list(messages)}, ensure_ascii=False) + '[/TO_USER]')


def task_call(action: str = "start", request: str = "查看材料") -> LLMResult:
    return LLMResult(content="内部工具解释，不应该发送", tool_calls=[{
        "id": "manage-1", "name": "background_task", "arguments": {"action": action, "request": request},
    }])


async def submit(scheduler: PrototypeScheduler, text: str, **kwargs: Any) -> str:
    return await scheduler.submit(text, conversation_id=KEY, actor_id="user:123", peer=PEER, **kwargs)


def visible(sender: ConsoleSender) -> list[str]:
    return [item["message"] for item in sender.sent]


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[Callable[..., Any]]:
    instances: list[PrototypeScheduler] = []

    async def build(model: Any, sender: Any = None, registry: ToolRegistry | None = None, **options: Any) -> PrototypeScheduler:
        index = len(instances)
        store = PrototypeStore(str(tmp_path / f"proto-{index}.sqlite"), media_dir=str(tmp_path / "media"))
        await store.initialize()
        settings = options.pop("settings", RuntimeSettings(wait_seconds=0.4, chat_timeout=1, send_timeout=0.4, worker_timeout=1))
        prepare = options.pop("prepare", None)
        if prepare is not None:
            await prepare(store)
        instance = PrototypeScheduler(store, model, sender or ConsoleSender(echo=False), worker_tools=registry or ToolRegistry(),
                                      prompts=PROMPTS, settings=settings, **options)
        instances.append(instance)
        await instance.start()
        return instance

    yield build
    for instance in reversed(instances):
        await asyncio.wait_for(instance.close(), 3)
        await instance.store.close()


async def test_empty_array_is_silence_but_invalid_response_is_error(factory: Callable[..., Any]) -> None:
    async def silent(messages: list[dict], tools: list[dict], *, lane: str) -> LLMResult:
        return reply()

    scheduler = await factory(silent)
    event = await submit(scheduler, "不用接话")
    await scheduler.wait_idle()
    assert visible(scheduler.sender) == []
    assert (await scheduler.store.get_event(event)).status == "no_reply"

    calls = 0

    async def invalid(messages: list[dict], tools: list[dict], *, lane: str) -> LLMResult:
        nonlocal calls
        calls += 1
        return LLMResult(content="")

    scheduler = await factory(invalid)
    event = await submit(scheduler, "坏格式")
    await scheduler.wait_idle()
    assert calls == 2
    assert visible(scheduler.sender) == [PROMPTS.error]
    assert (await scheduler.store.get_event(event)).status == "error"


async def test_wait_notice_cancels_only_generation_and_reuses_turn(factory: Callable[..., Any]) -> None:
    started = asyncio.Event()
    trigger_notice = asyncio.Event()
    cancelled = asyncio.Event()
    calls: list[list[dict]] = []

    async def sleep(delay: float) -> None:
        await trigger_notice.wait()

    async def model(messages: list[dict], tools: list[dict], *, lane: str) -> LLMResult:
        calls.append(messages)
        if len(calls) == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        assert PROMPTS.wait in str(messages)
        return reply("现在接上了。")

    scheduler = await factory(model, sleep=sleep)
    await submit(scheduler, "复杂问题")
    await asyncio.wait_for(started.wait(), 1)
    original_turn = scheduler.conversations[KEY].turn.id
    trigger_notice.set()
    await asyncio.wait_for(scheduler.wait_idle(), 2)
    assert cancelled.is_set()
    assert len(calls) == 2
    assert visible(scheduler.sender) == [PROMPTS.wait, "现在接上了。"]
    events = await scheduler.store.get_events(finalized_only=False)
    assert {event.turn_id for event in events if event.event_type in {"status_update", "outbound"}} == {original_turn}


async def test_notice_does_not_reset_total_deadline(factory: Callable[..., Any]) -> None:
    calls = 0

    async def model(messages: list[dict], tools: list[dict], *, lane: str) -> LLMResult:
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    settings = RuntimeSettings(wait_seconds=0.01, chat_timeout=0.08, send_timeout=0.2, worker_timeout=1)
    scheduler = await factory(model, settings=settings)
    event = await submit(scheduler, "模型一直不回")
    await asyncio.wait_for(scheduler.wait_idle(), 1)
    assert calls == 2
    assert visible(scheduler.sender) == [PROMPTS.wait, PROMPTS.error]
    assert (await scheduler.store.get_event(event)).status == "error"
    assert not scheduler.conversations[KEY].allow_tools.is_set()


async def test_no_notice_for_non_addressed_input(factory: Callable[..., Any]) -> None:
    async def model(messages: list[dict], tools: list[dict], *, lane: str) -> LLMResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    scheduler = await factory(model, settings=RuntimeSettings(wait_seconds=0.01, chat_timeout=0.05))
    await submit(scheduler, "群聊旁听", notify=False)
    await asyncio.wait_for(scheduler.wait_idle(), 1)
    assert visible(scheduler.sender) == []


async def test_started_send_finishes_but_rest_of_batch_stops(factory: Callable[..., Any]) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Sender(ConsoleSender):
        async def send(self, peer: Peer, message: str | list[dict]) -> dict:
            if message == "第一条":
                entered.set()
                await release.wait()
            return await super().send(peer, message)

    async def model(messages: list[dict], tools: list[dict], *, lane: str) -> LLMResult:
        return reply("新的话题") if "换个话题" in messages[-1]["content"] else reply("第一条", "第二条", "a | b")

    scheduler = await factory(model, Sender(echo=False))
    first = await submit(scheduler, "原来的话题")
    await asyncio.wait_for(entered.wait(), 1)
    await submit(scheduler, "换个话题")
    release.set()
    await asyncio.wait_for(scheduler.wait_idle(), 2)
    assert visible(scheduler.sender) == ["第一条", "新的话题"]
    assert (await scheduler.store.get_event(first)).status == "cancelled"
    outbound = [item.content for item in await scheduler.store.get_events() if item.event_type == "outbound"]
    assert outbound == ["第一条", "新的话题"]


@pytest.mark.parametrize("response", [{"status": "failed"}, {"status": "ok", "data": "malformed"}])
async def test_failed_nonempty_reply_is_never_silence(factory: Callable[..., Any], response: dict) -> None:
    class Sender:
        async def send(self, peer: Peer, message: str) -> dict:
            return response

    async def model(messages: list[dict], tools: list[dict], *, lane: str) -> LLMResult:
        return reply("有内容")

    scheduler = await factory(model, Sender())
    event = await submit(scheduler, "你好")
    await scheduler.wait_idle()
    assert (await scheduler.store.get_event(event)).status == "error"
    assert not scheduler.conversations[KEY].allow_tools.is_set()
    events = await scheduler.store.get_events()
    assert not any(item.event_type == "outbound" for item in events)
    assert any((item.payload or {}).get("outcome") == "failed" for item in events)


async def test_background_work_does_not_occupy_chat_and_never_speaks_directly(factory: Callable[..., Any]) -> None:
    working, release = asyncio.Event(), asyncio.Event()

    async def model(messages: list[dict], tools: list[dict], *, lane: str) -> LLMResult:
        if lane == "worker":
            working.set()
            await release.wait()
            return LLMResult(content="后台提供的资料")
        if messages[-1]["role"] == "tool":
            return reply("已经接下了。")
        current = messages[-1]["content"]
        if "开始任务" in current:
            return task_call()
        if current.startswith("平台后台结果到达"):
            return reply("结果到了。", "我来讲给你听。")
        return reply("我们继续聊天。")

    scheduler = await factory(model)
    original = await submit(scheduler, "开始任务，完整原话不可丢")
    await asyncio.wait_for(working.wait(), 1)
    await scheduler.wait_chat_idle(KEY)
    await submit(scheduler, "你怎么样")
    await scheduler.wait_chat_idle(KEY)
    assert visible(scheduler.sender) == ["已经接下了。", "我们继续聊天。"]
    assert not release.is_set()
    release.set()
    await asyncio.wait_for(scheduler.wait_idle(), 2)
    jobs = await scheduler.store.list_jobs(KEY)
    assert len(jobs) == 1 and jobs[0].delivered
    assert jobs[0].input_event_ids == [original]
    assert "完整原话不可丢" in jobs[0].request
    assert visible(scheduler.sender)[-2:] == ["结果到了。", "我来讲给你听。"]
    assert not any("后台提供的资料" == item or "内部工具" in item for item in visible(scheduler.sender))


async def test_stop_during_started_tool_preserves_tool_receipt(factory: Callable[..., Any]) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    was_cancelled = False
    executions = 0

    async def tool(args: dict) -> str:
        nonlocal was_cancelled, executions
        executions += 1
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            was_cancelled = True
            raise
        return "操作真实完成"

    registry = ToolRegistry()
    registry.register(Tool("controlled", "可控测试工具", {"type": "object"}, tool))

    async def model(messages: list[dict], tools: list[dict], *, lane: str) -> LLMResult:
        if lane == "worker":
            return LLMResult(content="不要把工具前解释发给人", tool_calls=[{"id": "work-1", "name": "controlled", "arguments": {}}])
        if messages[-1]["role"] == "tool":
            return reply("收到。")
        if messages[-1]["content"].startswith("平台后台结果到达"):
            return reply("未开始的工作已停止。")
        return task_call("cancel" if "停止任务" in messages[-1]["content"] else "start")

    scheduler = await factory(model, registry=registry)
    await submit(scheduler, "开始任务")
    await asyncio.wait_for(entered.wait(), 1)
    await scheduler.wait_chat_idle(KEY)
    await submit(scheduler, "停止任务")
    await scheduler.wait_chat_idle(KEY)
    assert not was_cancelled and not release.is_set()
    release.set()
    await asyncio.wait_for(scheduler.wait_idle(), 2)
    assert executions == 1 and not was_cancelled
    job = (await scheduler.store.list_jobs(KEY))[0]
    assert job.status == "cancelled" and not job.active_action
    receipts = [event for event in await scheduler.store.get_events() if (event.payload or {}).get("kind") == "worker_tool_result"]
    assert len(receipts) == 1 and receipts[0].content == "操作真实完成"


async def test_notice_after_task_acceptance_does_not_create_second_job(factory: Callable[..., Any]) -> None:
    final_started, notice = asyncio.Event(), asyncio.Event()
    worker_release = asyncio.Event()
    first_final = True

    async def sleep(delay: float) -> None:
        await notice.wait()

    async def model(messages: list[dict], tools: list[dict], *, lane: str) -> LLMResult:
        nonlocal first_final
        if lane == "worker":
            await worker_release.wait()
            return LLMResult(content="完成")
        if messages[-1]["content"].startswith("平台后台结果到达"):
            return reply("完成了。")
        if tools:
            return task_call()
        if first_final:
            first_final = False
            final_started.set()
            await asyncio.Event().wait()
        return reply("接到任务了。")

    scheduler = await factory(model, sleep=sleep)
    await submit(scheduler, "开始任务")
    await asyncio.wait_for(final_started.wait(), 1)
    notice.set()
    await scheduler.wait_chat_idle(KEY)
    assert len(await scheduler.store.list_jobs(KEY)) == 1
    assert visible(scheduler.sender) == [PROMPTS.wait, "接到任务了。"]
    worker_release.set()
    await asyncio.wait_for(scheduler.wait_idle(), 2)


async def test_unstarted_send_is_discarded_after_deadline(factory: Callable[..., Any]) -> None:
    entered, release, new_expired = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Sender(ConsoleSender):
        async def send(self, peer: Peer, message: str | list[dict]) -> dict:
            if message == "旧回复":
                entered.set()
                await release.wait()
            return await super().send(peer, message)

    async def model(messages: list[dict], tools: list[dict], *, lane: str) -> LLMResult:
        return reply("新回复") if "新输入" in messages[-1]["content"] else reply("旧回复")

    scheduler = await factory(model, Sender(echo=False), settings=RuntimeSettings(wait_seconds=0.3, chat_timeout=0.06, send_timeout=1))
    original_record = scheduler._record

    async def record(*args: Any, **kwargs: Any) -> EventRecord:
        value = await original_record(*args, **kwargs)
        if len(args) > 1 and args[1] == "chat_error":
            new_expired.set()
        return value

    scheduler._record = record
    await submit(scheduler, "旧输入")
    await asyncio.wait_for(entered.wait(), 1)
    await submit(scheduler, "新输入")
    await asyncio.wait_for(new_expired.wait(), 1)
    release.set()
    await asyncio.wait_for(scheduler.wait_idle(), 2)
    assert visible(scheduler.sender) == ["旧回复", PROMPTS.error]


async def test_deferred_result_is_not_marked_delivered(factory: Callable[..., Any]) -> None:
    async def model(messages: list[dict], tools: list[dict], *, lane: str) -> LLMResult:
        return reply()

    scheduler = await factory(model)
    job = await scheduler.store.create_job(conversation_id=KEY, actor_id="user:123", peer_chat_type=1,
                                           peer_uid="123", request="旧任务", input_event_ids=[])
    await scheduler.store.update_job(job.id, status="completed", result="仍待交付")
    await scheduler.start()
    await scheduler.wait_idle()
    assert not (await scheduler.store.get_job(job.id)).delivered
    assert visible(scheduler.sender) == []


async def test_shutdown_releases_not_yet_started_tool_without_executing(factory: Callable[..., Any]) -> None:
    about_to_finish = asyncio.Event()
    second_chat = asyncio.Event()
    calls = 0
    executed = False

    async def tool(args: dict) -> str:
        nonlocal executed
        executed = True
        return "不应执行"

    registry = ToolRegistry()
    registry.register(Tool("controlled", "test", {"type": "object"}, tool))

    async def model(messages: list[dict], tools: list[dict], *, lane: str) -> LLMResult:
        nonlocal calls
        if lane == "worker":
            about_to_finish.set()
            await second_chat.wait()
            return LLMResult(tool_calls=[{"id": "w", "name": "controlled", "arguments": {}}])
        calls += 1
        if calls == 1:
            return task_call()
        if calls == 2:
            return reply("已接收")
        second_chat.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    scheduler = await factory(model, registry=registry)
    await submit(scheduler, "开始")
    await asyncio.wait_for(about_to_finish.wait(), 1)
    await scheduler.wait_chat_idle(KEY)
    await submit(scheduler, "补充")
    await asyncio.wait_for(second_chat.wait(), 1)
    await asyncio.wait_for(scheduler.close(), 2)
    assert not executed


async def test_send_timeout_is_unknown_without_retry_or_false_silence(factory: Callable[..., Any]) -> None:
    attempts: list[str] = []

    class Sender:
        async def send(self, peer: Peer, message: str) -> dict:
            attempts.append(message)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def model(messages: list[dict], tools: list[dict], *, lane: str) -> LLMResult:
        return reply("是否送达尚不能确认", "这条不应接着发")

    scheduler = await factory(model, Sender(), settings=RuntimeSettings(
        wait_seconds=0.4, chat_timeout=1, send_timeout=0.02,
    ))
    event_id = await submit(scheduler, "发送后没有收到回执")
    await asyncio.wait_for(scheduler.wait_idle(), 2)
    assert attempts == ["是否送达尚不能确认"]
    assert (await scheduler.store.get_event(event_id)).status == "error"
    events = await scheduler.store.get_events(finalized_only=False)
    receipts = [event for event in events if (event.payload or {}).get("kind") == "send_result"]
    assert len(receipts) == 1 and receipts[0].payload["outcome"] == "unknown"
    assert not any(event.event_type == "outbound" for event in events)
    assert not scheduler.conversations[KEY].allow_tools.is_set()


async def test_restart_records_orphan_send_without_replaying_completed_input(factory: Callable[..., Any]) -> None:
    async def prepare(store: PrototypeStore) -> None:
        await store.append_event(EventRecord(
            conversation_id=KEY, actor_id="user:123", actor_kind="human", event_type="inbound", content="已经处理的输入",
            status="responded", payload={"prototype": True, "peer": PEER.model_dump()},
        ))
        await store.append_event(EventRecord(
            conversation_id=KEY, actor_id="bot", actor_kind="bot", event_type="state_change", content="发送过程中退出",
            turn_id="interrupted-send", payload={"prototype": True, "kind": "send_started", "source": "reply", "index": 0},
        ))

    async def model(messages: list[dict], tools: list[dict], *, lane: str) -> LLMResult:
        raise AssertionError("a completed input must not be replayed")

    scheduler = await factory(model, prepare=prepare)
    await scheduler.wait_idle()
    await scheduler.start()
    await scheduler.wait_idle()
    events = await scheduler.store.get_events(finalized_only=False)
    receipts = [event for event in events if (event.payload or {}).get("kind") == "send_result"]
    assert len(receipts) == 1
    assert receipts[0].turn_id == "interrupted-send" and receipts[0].payload["outcome"] == "unknown"
    assert visible(scheduler.sender) == []


@pytest.mark.parametrize("previous_status", ["queued", "running"])
async def test_restart_keeps_accepted_job_without_recreating_it(factory: Callable[..., Any], previous_status: str) -> None:
    input_id = ""
    job_id = ""
    worker_calls = 0
    chat_calls = 0

    async def prepare(store: PrototypeStore) -> None:
        nonlocal input_id, job_id
        event = await store.append_event(EventRecord(
            conversation_id=KEY, actor_id="user:123", actor_kind="human", event_type="inbound", content="原始任务",
            status="received", payload={"prototype": True, "peer": PEER.model_dump()},
        ))
        input_id = str(event.event_id)
        job = await store.create_job(conversation_id=KEY, actor_id="user:123", peer_chat_type=1,
                                     peer_uid="123", request="原始任务", input_event_ids=[input_id])
        job_id = job.id
        if previous_status == "running":
            await store.update_job(job.id, status="running", active_action="old-read:read_file")

    async def model(messages: list[dict], tools: list[dict], *, lane: str) -> LLMResult:
        nonlocal worker_calls, chat_calls
        if lane == "worker":
            worker_calls += 1
            return LLMResult(content="从队列恢复后完成")
        chat_calls += 1
        assert not tools
        assert messages[-1]["content"].startswith("平台后台结果到达")
        return reply("任务结果有更新。")

    scheduler = await factory(model, prepare=prepare)
    await asyncio.wait_for(scheduler.wait_idle(), 2)
    jobs = await scheduler.store.list_jobs(KEY)
    assert len(jobs) == 1 and jobs[0].id == job_id
    assert (await scheduler.store.get_event(input_id)).status == "cancelled"
    assert chat_calls == 1 and jobs[0].delivered
    assert worker_calls == (1 if previous_status == "queued" else 0)
    assert jobs[0].status == ("completed" if previous_status == "queued" else "unknown")
    assert jobs[0].active_action == ("" if previous_status == "queued" else "old-read:read_file")


async def test_user_update_discards_worker_report_waiting_for_chat(factory: Callable[..., Any]) -> None:
    worker_started, updating, old_report_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_update = asyncio.Event()
    worker_requests: list[list[dict]] = []

    async def model(messages: list[dict], tools: list[dict], *, lane: str) -> LLMResult:
        if lane == "worker":
            worker_requests.append(list(messages))
            if len(worker_requests) == 1:
                worker_started.set()
                await updating.wait()
                old_report_ready.set()
                return LLMResult(content="旧要求得出的结果，不可发布")
            assert "新增的完整限定条件" in str(messages)
            return LLMResult(content="按新要求重新完成")
        if messages[-1]["role"] == "tool":
            return reply("收到。")
        if messages[-1]["content"].startswith("平台后台结果到达"):
            assert "旧要求得出的结果，不可发布" not in str(messages)
            return reply("按补充要求处理好了。")
        if "修改要求" in messages[-1]["content"]:
            updating.set()
            await release_update.wait()
            return task_call("update", "模型简写的新要求")
        return task_call()

    scheduler = await factory(model)
    first_id = await submit(scheduler, "开始任务")
    await asyncio.wait_for(worker_started.wait(), 1)
    await scheduler.wait_chat_idle(KEY)
    update_id = await submit(scheduler, "修改要求：新增的完整限定条件")
    await asyncio.wait_for(old_report_ready.wait(), 1)
    assert not scheduler.conversations[KEY].allow_tools.is_set()
    release_update.set()
    await asyncio.wait_for(scheduler.wait_idle(), 2)
    job = (await scheduler.store.list_jobs(KEY))[0]
    assert len(worker_requests) == 2 and job.result == "按新要求重新完成"
    assert job.input_event_ids == [first_id, update_id]
    assert "新增的完整限定条件" in job.updates[0]
    reports = [event.content for event in await scheduler.store.get_events() if (event.payload or {}).get("kind") == "task_report"]
    assert reports == ["按新要求重新完成"]
