"""Regression coverage for the production conversation/Discord routing audit."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord_notifier
import httpx
import pytest
from auth_deps import get_guild_pk
from db import github_cache
from db.github_events import link_task_context, list_events_by_conversation
from discord import router
from foreman import runner, tools
from foreman.conversation_service import create_conversation
from helpers import insert_guild, insert_worker
from models import GithubEvent, Task
from routes.debug_query import _validate_readonly_query
from sqlmodel import select


@pytest.mark.parametrize(
    "table",
    [
        "conversations",
        "messages",
        "github_issues",
        "github_pull_requests",
        "discord_thread_bindings",
        "discord_channel_guilds",
    ],
)
def test_debug_query_allows_conversation_operational_tables(table):
    assert _validate_readonly_query(f"SELECT * FROM {table}")


@pytest.mark.parametrize("result", [[{"is_error": True, "content": "Worker offline"}], []])
async def test_failed_direct_route_falls_back_to_foreman(result):
    with (
        patch.object(router, "_persist_inbound_message", AsyncMock(return_value=123)),
        patch.object(
            router, "_task_state_row", AsyncMock(return_value=("working", "execute", "w-test"))
        ),
        patch.object(tools, "exec_tools", AsyncMock(return_value=result)),
        patch.object(router.triggers, "trigger_foreman", AsyncMock()) as trigger,
        patch.object(runner, "reset_foreman_poll"),
    ):
        await router._forward_to_foreman(
            "guild",
            "fix this",
            "[Discord] user: fix this",
            ps_user_id="user",
            task_id="t-test",
            reply_channel_id="thread",
            task_name="test",
        )
    trigger.assert_awaited_once()
    assert trigger.call_args.kwargs["conversation_id"] == 123
    assert "may have partially applied" in trigger.call_args.args[2]


async def test_run_forwards_its_conversation_to_real_tool_executor():
    from foreman.run import ForemanRun, RealToolExecutor, RunConfig
    from test_foreman_run import (
        FakeHistory,
        FakeLLM,
        RecordingJournal,
        _text_block,
        _tool_use_block,
    )

    tool = _tool_use_block("create_task", "pinned-tool", {"name": "Pinned"})
    run = ForemanRun(
        RunConfig(
            guild_id="guild",
            user_id="user",
            task_id=None,
            trigger="chat",
            max_rounds=2,
            conversation_id=321,
        ),
        llm=FakeLLM([[tool], [_text_block("Done")]]),
        tools=RealToolExecutor(),
        journal=RecordingJournal(),
        history=FakeHistory(),
    )
    with patch.object(
        tools,
        "exec_tools",
        AsyncMock(
            return_value=[
                {
                    "type": "tool_result",
                    "tool_use_id": "pinned-tool",
                    "content": "Created",
                }
            ]
        ),
    ) as execute:
        await run.execute("Create task", system_blocks=[], state_preamble="", audit_system="")
    execute.assert_awaited_once_with("guild", [tool], user_id="user", conversation_id=321)


async def test_context_failure_still_drains_queued_discord_reply():
    key = ("g-context-failure", "user")
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def run(*args, **kwargs):
        calls.append((args[1], kwargs.get("conversation_id")))
        if args[1] == "first":
            started.set()
            await release.wait()
            raise RuntimeError("context loading failed")

    try:
        with patch.object(runner, "_run_foreman_ai", run):
            first = asyncio.create_task(
                runner.run_foreman_ai(
                    key[0],
                    "first",
                    user_id=key[1],
                    is_human=True,
                    conversation_id=123,
                )
            )
            await started.wait()
            await runner.run_foreman_ai(
                key[0],
                "second",
                user_id=key[1],
                is_human=True,
                conversation_id=123,
            )
            release.set()
            with pytest.raises(RuntimeError, match="context loading failed"):
                await first
        assert len(calls) == 2
        assert calls[1][0].endswith("second")
        assert calls[1][1] == 123
        assert key not in runner._human_queues
        assert key not in runner._guild_locks
    finally:
        runner._human_queues.pop(key, None)
        runner._guild_locks.pop(key, None)


@pytest.mark.parametrize("tool_name", ["create_task", "assign_task"])
async def test_task_creation_keeps_pinned_conversation(client, tool_name):
    import database

    _, db_url = client
    slug = f"g-pinned-tool-{tool_name}"
    insert_guild(db_url, slug)
    insert_worker(db_url, slug, f"w-pinned-{tool_name}")
    async with database.AsyncSessionLocal() as db:
        guild_pk = await get_guild_pk(db, slug)
        original = await create_conversation(db, guild_pk, "user")
        original_id = original.id
        await create_conversation(db, guild_pk, "user")
        await db.commit()

    tool = SimpleNamespace(
        id="create-pinned",
        name=tool_name,
        input={"name": "Pinned task", "description": "Do it", "worker_id": f"w-pinned-{tool_name}"},
    )
    with patch("foreman.tools.broadcast", AsyncMock()):
        result = await tools.exec_tools(slug, [tool], user_id="user", conversation_id=original_id)
    assert not result[0].get("is_error"), result
    async with database.AsyncSessionLocal() as db:
        task = (await db.exec(select(Task).where(Task.guild_id == guild_pk))).one()
        assert task.conversation_id == original_id


async def test_task_tool_rejects_conversation_from_another_guild(client):
    import database

    _, db_url = client
    insert_guild(db_url, "g-tool-target")
    insert_guild(db_url, "g-tool-foreign")
    async with database.AsyncSessionLocal() as db:
        foreign_pk = await get_guild_pk(db, "g-tool-foreign")
        conversation = await create_conversation(db, foreign_pk, "user")
        conversation_id = conversation.id
        await db.commit()
    result = await tools.exec_tools(
        "g-tool-target",
        [
            SimpleNamespace(
                id="cross-guild",
                name="create_task",
                input={"name": "Must not create"},
            )
        ],
        user_id="user",
        conversation_id=conversation_id,
    )
    assert result[0]["is_error"]
    assert "does not belong" in result[0]["content"]


async def test_pr_linkage_reconciles_old_events_and_shares_context(client):
    import database

    _, db_url = client
    slug = "g-reconcile-old-events"
    insert_guild(db_url, slug)
    insert_guild(db_url, "g-reconcile-other-guild")
    async with database.AsyncSessionLocal() as db:
        guild_pk = await get_guild_pk(db, slug)
        other_guild_pk = await get_guild_pk(db, "g-reconcile-other-guild")
        first = await create_conversation(db, guild_pk, "user")
        second = await create_conversation(db, guild_pk, "user")
        first_id, second_id = first.id, second.id
        await github_cache.upsert_pr(db, "owner/shared", {"number": 42, "title": "Shared PR"})
        for guild, delivery in ((guild_pk, "local-early"), (other_guild_pk, "foreign-early")):
            db.add(
                GithubEvent(
                    guild_id=guild,
                    delivery_id=delivery,
                    event_type="pull_request",
                    action="opened",
                    repo="owner/shared",
                    pr_number=42,
                    payload_json="{}",
                    created_at=datetime.now(UTC),
                )
            )
        await db.commit()
        for task_id, conv in (("t-shared-first", first_id), ("t-shared-second", second_id)):
            db.add(
                Task(
                    id=task_id,
                    guild_id=guild_pk,
                    conversation_id=conv,
                    name="Shared",
                    description="Shared work",
                    pr_repo="owner/shared",
                    pr_number=42,
                    state="pending",
                    created_at=datetime.now(UTC),
                )
            )
            await db.flush()
            await link_task_context(db, task_id)
        await db.commit()
        for conv in (first_id, second_id):
            events = await list_events_by_conversation(db, conv)
            assert [event.delivery_id for event in events] == ["local-early"]
            context = json.loads(await runner._load_conversation_context(db, conv))
            assert context["github_events"][0]["pr_number"] == 42
            assert context["pull_requests"][0]["number"] == 42
        cached = await github_cache.get_pr(db, "owner/shared", 42)
        assert cached.conversation_id == first_id
        foreign = (
            await db.exec(select(GithubEvent).where(GithubEvent.delivery_id == "foreign-early"))
        ).one()
        assert foreign.conversation_id is None
        assert foreign.task_id is None


@pytest.mark.parametrize(
    "tool_name,model",
    [
        ("create_github_issue", "issue"),
        ("create_pr", "pr"),
    ],
)
async def test_remote_creation_records_conversation_before_task_exists(client, tool_name, model):
    import database

    _, db_url = client
    slug = f"g-remote-create-{model}"
    insert_guild(db_url, slug)
    async with database.AsyncSessionLocal() as db:
        guild_pk = await get_guild_pk(db, slug)
        conversation = await create_conversation(db, guild_pk, "user")
        conversation_id = conversation.id
        await db.commit()
    payload = {"number": 99, "title": "Created", "html_url": "https://github.com/owner/repo/99"}
    tool = SimpleNamespace(
        id="remote-create",
        name=tool_name,
        input={
            "repo": "owner/repo",
            "title": "Created",
            "branch": "fix",
            "body": "Details",
        },
    )
    with (
        patch.object(tools, "_guild_github_token", AsyncMock(return_value=("fake", "user"))),
        patch.object(tools, "_to_thread", AsyncMock(return_value=payload)),
    ):
        result = await tools.exec_tools(
            slug, [tool], user_id="user", conversation_id=conversation_id
        )
    assert not result[0].get("is_error"), result
    async with database.AsyncSessionLocal() as db:
        getter = github_cache.get_issue if model == "issue" else github_cache.get_pr
        cached = await getter(db, "owner/repo", 99)
        assert cached.conversation_id == conversation_id


async def test_webhook_batch_is_queued_while_another_conversation_is_running():
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def run(*args, **kwargs):
        calls.append((args[1], kwargs.get("conversation_id")))
        if args[1] == "first":
            started.set()
            await release.wait()

    with patch.object(runner, "_run_foreman_ai", run):
        first = asyncio.create_task(
            runner.run_foreman_ai(
                "g-queued-webhook",
                "first",
                user_id="user",
                conversation_id=123,
            )
        )
        await started.wait()
        await runner.run_foreman_ai(
            "g-queued-webhook",
            "review comment",
            user_id="user",
            conversation_id=456,
            trigger="github-event",
        )
        release.set()
        await first
    assert len(calls) == 2
    assert calls[1][0].endswith("review comment")
    assert calls[1][1] == 456


def test_webhook_queue_overflow_does_not_evict_human_messages(monkeypatch):
    key = ("g-overflow-priority", "user")
    monkeypatch.setattr(runner, "_HUMAN_QUEUE_MAX", 2)
    try:
        for message in ("human one", "human two"):
            runner._enqueue_human_turn(key, key[0], message, "", key[1], None, trigger="chat")
        runner._enqueue_human_turn(
            key, key[0], "CI batch", "", key[1], None, trigger="github-event"
        )
        assert [turn.human_message for turn in runner._human_queues[key]] == [
            "human one",
            "human two",
        ]
    finally:
        runner._human_queues.pop(key, None)


async def test_discord_rate_limit_retries_same_destination(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")
    request = httpx.Request("POST", "https://discord.com/api/channels/thread/messages")
    limited = httpx.Response(429, headers={"Retry-After": "0.25"}, request=request)
    error = httpx.HTTPStatusError("Rate limited", request=request, response=limited)
    with (
        patch.object(
            discord_notifier, "_bot_request_raw", AsyncMock(side_effect=[error, {"id": "sent"}])
        ) as raw,
        patch.object(discord_notifier.asyncio, "sleep", AsyncMock()) as sleep,
    ):
        result = await discord_notifier.post("/channels/thread/messages", {"content": "Answer"})
    assert result == {"id": "sent"}
    sleep.assert_awaited_once_with(0.25)
    assert raw.await_args_list[0] == raw.await_args_list[1]


async def test_webhook_routes_via_cached_conversation_without_task(client):
    import database
    from test_github_webhooks_phase2 import _pr_payload, _set_webhook_secret, _signed_headers

    test_client, db_url = client
    slug = "g-cache-route-no-task"
    insert_guild(db_url, slug)
    _set_webhook_secret(db_url, slug, "test-secret")
    async with database.AsyncSessionLocal() as db:
        guild_pk = await get_guild_pk(db, slug)
        original = await create_conversation(db, guild_pk, "user")
        original_id = original.id
        await create_conversation(db, guild_pk, "user")
        await github_cache.upsert_pr(
            db, "owner/routed", {"number": 77}, conversation_id=original_id
        )
    body = json.dumps(_pr_payload(action="edited", repo="owner/routed", number=77)).encode()
    with patch("routes.webhooks._debounce_queue") as queue:
        queue.schedule = AsyncMock()
        response = test_client.post(
            f"/webhooks/github/{slug}",
            content=body,
            headers=_signed_headers(
                "test-secret", body, event="pull_request", delivery="cache-routed-event"
            ),
        )
    assert response.status_code == 202, response.text
    queue.schedule.assert_awaited_once()
    assert queue.schedule.call_args.kwargs["conversation_id"] == original_id
    async with database.AsyncSessionLocal() as db:
        event = (
            await db.exec(
                select(GithubEvent).where(GithubEvent.delivery_id == "cache-routed-event")
            )
        ).one()
        assert event.conversation_id == original_id
        assert event.task_id is None


async def test_unowned_review_request_creates_pr_context_not_latest_chat(client):
    import database
    from test_github_webhooks_phase2 import (
        _pr_payload,
        _set_guild_owner_token,
        _set_webhook_secret,
        _signed_headers,
    )

    test_client, db_url = client
    slug = "g-review-new-context"
    insert_guild(db_url, slug, owner_user_id="review-owner")
    _set_guild_owner_token(db_url, "review-owner", "review-owner")
    _set_webhook_secret(db_url, slug, "review-secret")
    async with database.AsyncSessionLocal() as db:
        guild_pk = await get_guild_pk(db, slug)
        unrelated = await create_conversation(db, guild_pk, "review-owner")
        unrelated_id = unrelated.id
        await db.commit()
    body = json.dumps(
        _pr_payload(
            action="review_requested",
            repo="owner/reviewed",
            number=88,
            requested_reviewer={"login": "review-owner"},
        )
    ).encode()
    headers = _signed_headers(
        "review-secret", body, event="pull_request", delivery="new-review-context"
    )
    with patch("routes.webhooks._debounce_queue") as queue:
        queue.schedule = AsyncMock()
        first = test_client.post(f"/webhooks/github/{slug}", content=body, headers=headers)
        duplicate = test_client.post(f"/webhooks/github/{slug}", content=body, headers=headers)
    assert first.status_code == duplicate.status_code == 202
    queue.schedule.assert_awaited_once()
    conversation_id = queue.schedule.call_args.kwargs["conversation_id"]
    assert conversation_id is not None
    assert conversation_id != unrelated_id
    async with database.AsyncSessionLocal() as db:
        cached = await github_cache.get_pr(db, "owner/reviewed", 88)
        event = (
            await db.exec(
                select(GithubEvent).where(GithubEvent.delivery_id == "new-review-context")
            )
        ).one()
        assert cached.conversation_id == event.conversation_id == conversation_id
