"""Conversation execution boundaries for maintenance and shared PR activity."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
import pytest
from auth_deps import get_guild_pk
from db import github_cache
from db.github_events import list_events_by_conversation
from foreman import runner, tools
from foreman.conversation_service import create_conversation
from foreman.run import ForemanRun, RunConfig
from helpers import insert_guild, insert_worker, truncate_all
from models import ConversationTask, GithubEvent, Task, task_conversation_id
from routes import webhooks
from sqlmodel import select
from test_foreman_run import (
    FakeHistory,
    FakeLLM,
    RecordingJournal,
    ScriptedToolExecutor,
    _text_block,
    _tool_use_block,
)
from test_github_webhooks_phase2 import _pr_payload, _set_webhook_secret, _signed_headers


async def _work(db_url):
    truncate_all(db_url)
    slug = "g-execution-scope"
    insert_guild(db_url, slug, owner_user_id="scope-owner")
    async with database.AsyncSessionLocal() as db:
        guild_pk = await get_guild_pk(db, slug)
        first = await create_conversation(db, guild_pk, "scope-owner")
        second = await create_conversation(db, guild_pk, "scope-owner")
        ids = (first.id, second.id)
        for task_id, name, conversation_id in (
            ("t-scope-first", "ATTACHED_TASK", first.id),
            ("t-scope-second", "UNRELATED_TASK", second.id),
        ):
            db.add(
                Task(
                    id=task_id,
                    guild_id=guild_pk,
                    name=name,
                    description=name,
                    state="working",
                    pr_repo="owner/scoped",
                    pr_number=42,
                    created_at=datetime.now(UTC),
                    user_id="scope-owner",
                )
            )
            await db.flush()
            db.add(ConversationTask(conversation_id=conversation_id, task_id=task_id))
        await db.commit()
    return slug, ids


@pytest.mark.parametrize("maintenance", [False, True])
async def test_runner_uses_execution_scope(client, maintenance):
    _, db_url = client
    slug, (first, _) = await _work(db_url)
    execute = AsyncMock()
    with (
        patch.object(runner, "HAS_ANTHROPIC", True),
        patch.object(runner, "_get_guild_user_id", AsyncMock(return_value="scope-owner")),
        patch.object(runner, "_load_foreman_config", AsyncMock(return_value={})),
        patch.object(
            runner, "resolve_foreman_client", AsyncMock(return_value=(None, "anthropic", "fake"))
        ),
        patch.object(
            runner, "ForemanRun", return_value=SimpleNamespace(execute=execute)
        ) as factory,
        patch.object(
            runner, "resolve_conversation_id", wraps=runner.resolve_conversation_id
        ) as resolve,
    ):
        await runner._run_foreman_ai(
            slug,
            "Check work",
            conversation_id=None if maintenance else first,
            trigger="periodic-check" if maintenance else "conversation-message",
        )
    cfg = factory.call_args.args[0]
    assert cfg.conversation_id == (None if maintenance else first)
    resolve.assert_not_called()
    preamble = execute.call_args.kwargs["state_preamble"]
    assert "ATTACHED_TASK" in preamble
    assert ("UNRELATED_TASK" in preamble) == maintenance


async def test_maintenance_does_not_load_user_history():
    history = SimpleNamespace(load_for_llm=AsyncMock(side_effect=AssertionError("history loaded")))
    llm = FakeLLM([[_text_block("Maintenance complete")]])
    run = ForemanRun(
        RunConfig("guild", "owner", None, "periodic-check", 2),
        llm=llm,
        tools=ScriptedToolExecutor({}),
        journal=RecordingJournal(),
        history=history,
    )
    await run.execute("Poll work", system_blocks=[], state_preamble="", audit_system="")
    history.load_for_llm.assert_not_called()
    messages = llm.calls[0]["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"][-1]["text"] == "Poll work"


@pytest.mark.parametrize("tool_name", ["create_task", "assign_task"])
async def test_unscoped_task_creation_does_not_reuse_latest_chat(client, tool_name):
    _, db_url = client
    slug, ids = await _work(db_url)
    insert_worker(db_url, slug, "w-scope")
    tool = SimpleNamespace(
        id="new-task",
        name=tool_name,
        input={
            "name": "Maintenance discovered work",
            "description": "Do it",
            "worker_id": "w-scope",
        },
    )
    result = await tools.exec_tools(slug, [tool], user_id="scope-owner")
    assert not result[0].get("is_error"), result
    async with database.AsyncSessionLocal() as db:
        conversation_id = (
            await db.exec(
                select(task_conversation_id()).where(Task.name == "Maintenance discovered work")
            )
        ).one()
        assert conversation_id is not None
        assert conversation_id not in ids


async def test_completed_task_keeps_shared_pr_context(client):
    _, db_url = client
    slug, ids = await _work(db_url)
    async with database.AsyncSessionLocal() as db:
        guild_pk = await get_guild_pk(db, slug)
        await github_cache.upsert_pr(
            db, "owner/scoped", {"number": 42, "title": "Shared PR"}, conversation_id=ids[0]
        )
        db.add(
            GithubEvent(
                guild_id=guild_pk,
                delivery_id="shared-finished",
                event_type="pull_request",
                repo="owner/scoped",
                pr_number=42,
                payload_json="{}",
                conversation_id=ids[0],
                created_at=datetime.now(UTC),
            )
        )
        task = await db.get(Task, "t-scope-second")
        task.state = "done"
        task.finished_at = datetime.now(UTC) - timedelta(days=3)
        await db.commit()
        assert [e.delivery_id for e in await list_events_by_conversation(db, ids[1])] == [
            "shared-finished"
        ]
        context = json.loads(await runner._load_conversation_context(db, ids[1]))
        assert context["pull_requests"][0]["number"] == 42
        assert context["github_events"][0]["pr_number"] == 42


async def test_webhook_targets_all_attached_conversations_including_completed(client):
    http, db_url = client
    slug, ids = await _work(db_url)
    _set_webhook_secret(db_url, slug, "scope-secret")
    async with database.AsyncSessionLocal() as db:
        task = await db.get(Task, "t-scope-second")
        task.finished_at = datetime.now(UTC)
        task.state = "done"
        db.add(ConversationTask(conversation_id=ids[1], task_id="t-scope-first"))
        await db.commit()
    body = json.dumps(_pr_payload(action="edited", repo="owner/scoped", number=42)).encode()
    with patch.object(webhooks, "_debounce_queue") as queue:
        queue.schedule = AsyncMock()
        response = http.post(
            f"/webhooks/github/{slug}",
            content=body,
            headers=_signed_headers(
                "scope-secret",
                body,
                event="pull_request",
                delivery="scope-fanout",
            ),
        )
    assert response.status_code == 202, response.text
    call = queue.schedule.call_args
    assert {cid for cid, _ in call.kwargs["related_conversations"]} == set(ids)


async def test_webhook_reuses_local_attachments_despite_new_unlinked_task_and_foreign_cache(client):
    http, db_url = client
    slug, ids = await _work(db_url)
    _set_webhook_secret(db_url, slug, "scope-secret")
    insert_guild(db_url, "g-foreign-scope", owner_user_id="foreign-owner")
    async with database.AsyncSessionLocal() as db:
        foreign_pk = await get_guild_pk(db, "g-foreign-scope")
        foreign = await create_conversation(db, foreign_pk, "foreign-owner")
        guild_pk = await get_guild_pk(db, slug)
        db.add(
            Task(
                id="t-new-unlinked",
                guild_id=guild_pk,
                name="New work",
                description="Do it",
                pr_repo="owner/scoped",
                pr_number=42,
                created_at=datetime.now(UTC) + timedelta(seconds=1),
            )
        )
        await github_cache.upsert_pr(db, "owner/scoped", {"number": 42}, conversation_id=foreign.id)
    body = json.dumps(_pr_payload(action="edited", repo="owner/scoped", number=42)).encode()
    with (
        patch.object(webhooks, "_debounce_queue") as queue,
        patch.object(webhooks, "ensure_conversation", AsyncMock()) as ensure,
    ):
        queue.schedule = AsyncMock()
        response = http.post(
            f"/webhooks/github/{slug}",
            content=body,
            headers=_signed_headers(
                "scope-secret",
                body,
                event="pull_request",
                delivery="existing-scope",
            ),
        )
    assert response.status_code == 202, response.text
    ensure.assert_not_awaited()
    assert queue.schedule.call_args.kwargs["conversation_id"] == ids[0]
    assert {cid for cid, _ in queue.schedule.call_args.kwargs["related_conversations"]} == set(ids)
    async with database.AsyncSessionLocal() as db:
        linked = (
            await db.exec(select(task_conversation_id()).where(Task.id == "t-new-unlinked"))
        ).one()
        assert linked == ids[0]


@pytest.mark.parametrize("event", ["pull_request_review", "issue_comment"])
async def test_unowned_pr_activity_creates_and_reuses_a_conversation(client, event):
    http, db_url = client
    slug, unrelated = await _work(db_url)
    _set_webhook_secret(db_url, slug, "scope-secret")
    payload = _pr_payload(action="submitted", repo="owner/new", number=88)
    if event == "issue_comment":
        payload["action"] = "created"
        payload["issue"] = payload.pop("pull_request")
        payload["issue"]["pull_request"] = {
            "url": "https://api.github.com/repos/owner/new/pulls/88"
        }
    body = json.dumps(payload).encode()
    with patch.object(webhooks, "_debounce_queue") as queue:
        queue.schedule = AsyncMock()
        for delivery in ("scope-new-1", "scope-new-2"):
            response = http.post(
                f"/webhooks/github/{slug}",
                content=body,
                headers=_signed_headers(
                    "scope-secret",
                    body,
                    event=event,
                    delivery=delivery,
                ),
            )
            assert response.status_code == 202, response.text
    calls = queue.schedule.call_args_list
    assert len(calls) == 2
    new_id = calls[0].kwargs["conversation_id"]
    assert new_id is not None
    assert new_id not in unrelated
    assert calls[1].kwargs["conversation_id"] == new_id


async def test_shared_webhook_has_one_action_run_and_an_observer_per_other_conversation():
    queue = webhooks.DebounceQueue()
    with (
        patch.object(webhooks, "run_foreman_ai", AsyncMock()) as execute,
        patch.object(webhooks, "ensure_poll_loop"),
    ):
        await queue._deliver(
            "guild:task",
            "guild",
            [
                ("Review feedback", "owner", "task", 10, ((10, "owner"), (20, "other"))),
                ("CI completed", "owner", "task", 10, ((10, "owner"), (20, "other"))),
            ],
        )
    assert [(c.kwargs["conversation_id"], c.kwargs["trigger"]) for c in execute.call_args_list] == [
        (10, "github-event"),
        (20, "github-event-observer"),
    ]


async def test_webhook_observer_runs_in_its_own_history_without_tools():
    llm = FakeLLM([[_text_block("The attached PR received feedback")]])
    journal = RecordingJournal()
    history = SimpleNamespace(
        load_for_llm=AsyncMock(
            return_value=[
                {
                    "role": "user",
                    "content": "This conversation's work",
                }
            ]
        )
    )
    run = ForemanRun(
        RunConfig("guild", "other", None, "github-event-observer", 2, conversation_id=20),
        llm=llm,
        tools=ScriptedToolExecutor({}),
        journal=journal,
        history=history,
    )
    await run.execute("Review feedback", system_blocks=[], state_preamble="", audit_system="")
    history.load_for_llm.assert_awaited_once_with("guild", "other", 20)
    assert llm.calls[0]["tools"] == []
    assert ("text", "The attached PR received feedback") in journal.calls


async def test_webhook_observer_cannot_mutate_shared_tasks():
    llm = FakeLLM([[_tool_use_block("send_followup", "forbidden", {"task_id": "shared"})]])
    executor = ScriptedToolExecutor({})
    run = ForemanRun(
        RunConfig("guild", "owner", None, "github-event-observer", 2, conversation_id=20),
        llm=llm,
        tools=executor,
        journal=RecordingJournal(),
        history=FakeHistory(),
    )
    with pytest.raises(ValueError, match="observers cannot execute tools"):
        await run.execute("Review feedback", system_blocks=[], state_preamble="", audit_system="")
    assert llm.calls[0]["tools"] == []
    assert executor.batches == []
