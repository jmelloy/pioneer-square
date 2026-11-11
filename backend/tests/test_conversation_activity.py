"""A conversation consumes output from all its tasks, live and after reload."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import database
import discord_notifier
import pytest
from conversation_activity import github_activity
from helpers import insert_conversation, insert_guild, insert_task, make_auth_token
from models import Guild, Message
from sqlmodel import select
from test_github_webhooks import _insert_task_with_worker, _set_webhook_secret, _signed_headers
from ws_handlers import WSContext, handle_terminal_output
from ws_types import TerminalOutputMsg


async def test_history_paginates_with_tied_timestamps_and_rejects_foreign_cursor(client):
    test_client, db_url = client
    slug = "g-activity-pages"
    insert_guild(db_url, slug)
    conv_id = insert_conversation(db_url, slug)
    other_id = insert_conversation(db_url, slug)
    async with database.AsyncSessionLocal() as db:
        guild_pk = (await db.exec(select(Guild.id).where(Guild.slug == slug))).one()
        for i in range(103):
            db.add(
                Message(
                    guild_id=guild_pk,
                    conversation_id=conv_id,
                    from_agent="worker",
                    content=f"Output {i}",
                    message_type="activity",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
        foreign = Message(
            guild_id=guild_pk,
            conversation_id=other_id,
            from_agent="user",
            content="private",
            message_type="chat",
            created_at=datetime.now(UTC),
        )
        db.add(foreign)
        await db.commit()
        foreign_id = foreign.id
    headers = {"Authorization": f"Bearer {make_auth_token(db_url)}"}
    url = f"/api/guilds/{slug}/conversations/{conv_id}/messages"
    page = test_client.get(url, headers=headers).json()
    assert len(page) == 100
    earlier = test_client.get(url, params={"before_id": page[0]["id"]}, headers=headers).json()
    assert [m["content"] for m in earlier] == ["Output 0", "Output 1", "Output 2"]
    assert (
        test_client.get(url, params={"before_id": foreign_id}, headers=headers).status_code == 404
    )


@pytest.mark.parametrize("conclusion", ["success", "failure", "cancelled"])
def test_check_output_includes_report_and_link(conclusion):
    content = github_activity(
        "check_run",
        "completed",
        {
            "check_run": {
                "name": "pytest",
                "status": "completed",
                "conclusion": conclusion,
                "output": {"title": "Tests", "summary": "42 passed", "text": "full report"},
                "html_url": "https://github.com/org/repo/runs/1",
            }
        },
    )
    assert f"pytest: {conclusion}" in content
    assert "42 passed" in content
    assert "full report" in content
    assert "https://github.com/org/repo/runs/1" in content


def test_pending_check_is_not_timeline_noise():
    assert github_activity("check_run", "created", {"check_run": {"status": "queued"}}) is None


async def test_worker_output_from_multiple_tasks_is_live_and_durable(client):
    test_client, db_url = client
    slug = "g-activity-workers"
    insert_guild(db_url, slug)
    conversation_id = insert_conversation(db_url, slug)
    other_id = insert_conversation(db_url, slug)
    for task_id, conv_id in [
        ("t-feed-1", conversation_id),
        ("t-feed-2", conversation_id),
        ("t-feed-3", other_id),
    ]:
        insert_task(db_url, slug, task_id, conversation_id=conv_id)
    with (
        patch("conversation_activity.broadcast_msg", new_callable=AsyncMock) as broadcast,
        patch.object(discord_notifier, "notify_task_stream", new_callable=AsyncMock) as stream,
    ):
        async with database.AsyncSessionLocal() as db:
            guild_pk = (await db.exec(select(Guild.id).where(Guild.slug == slug))).one()
            ctx = WSContext(websocket=AsyncMock(), db=db, guild_id=slug, guild_pk=guild_pk)
            for task_id in ["t-feed-1", "t-feed-2", "t-feed-3"]:
                await handle_terminal_output(
                    ctx,
                    TerminalOutputMsg(
                        taskId=task_id,
                        line=f"Working on {task_id}",
                        level="info",
                        detail={
                            "toolType": "tool_use",
                            "name": "bash",
                            "input": {"command": "pytest"},
                        },
                    ),
                )
        assert [call.args[1].conversationId for call in broadcast.call_args_list] == [
            conversation_id,
            conversation_id,
            other_id,
        ]
        assert stream.call_args_list[0].kwargs["conversation_id"] == conversation_id
        assert broadcast.call_args_list[0].args[1].detail["input"] == {"command": "pytest"}
        assert broadcast.call_args_list[0].args[1].level == "info"
    token = make_auth_token(db_url)
    response = test_client.get(
        f"/api/guilds/{slug}/conversations/{conversation_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert [m["taskId"] for m in response.json()] == ["t-feed-1", "t-feed-2"]
    assert all(m["source"] == "worker" for m in response.json())
    assert all(m["detail"]["input"] == {"command": "pytest"} for m in response.json())
    assert all(m["level"] == "info" for m in response.json())


def test_successful_check_is_persisted_once_for_conversation(client):
    test_client, db_url = client
    slug = "g-activity-check"
    insert_guild(db_url, slug)
    conv_id = insert_conversation(db_url, slug)
    _insert_task_with_worker(
        db_url,
        task_id="t-feed-check",
        guild_id=slug,
        pr_repo="org/repo",
        pr_number=41,
        conversation_id=conv_id,
    )
    _set_webhook_secret(db_url, slug, "secret")
    payload = {
        "action": "completed",
        "repository": {"full_name": "org/repo"},
        "check_run": {
            "name": "pytest",
            "status": "completed",
            "conclusion": "success",
            "pull_requests": [{"number": 41, "html_url": "https://github.com/org/repo/pull/41"}],
            "output": {"summary": "42 tests passed"},
        },
    }
    body = json.dumps(payload).encode()
    headers = _signed_headers("secret", body, event="check_run", delivery="feed-check-delivery")
    for _ in range(2):
        assert (
            test_client.post(f"/webhooks/github/{slug}", content=body, headers=headers).status_code
            == 202
        )
    response = test_client.get(
        f"/api/guilds/{slug}/conversations/{conv_id}/messages",
        headers={"Authorization": f"Bearer {make_auth_token(db_url)}"},
    )
    messages = response.json()
    assert len(messages) == 1
    assert messages[0]["source"] == "github"
    assert messages[0]["taskId"] == "t-feed-check"
    assert "42 tests passed" in messages[0]["content"]


def test_ci_notify_broadcasts_conversation_and_task(client, monkeypatch):
    test_client, db_url = client
    slug = "g-activity-ci-notify"
    insert_guild(db_url, slug)
    conv_id = insert_conversation(db_url, slug)
    insert_task(db_url, slug, "t-feed-notify", conversation_id=conv_id)
    monkeypatch.setenv("PIONEER_CI_KEY", "feed-key")
    with patch("routes.webhooks.broadcast_msg", new_callable=AsyncMock) as broadcast:
        response = test_client.post(
            f"/guilds/{slug}/foreman/ci-notify",
            json={
                "repo": "org/repo",
                "workflow_name": "CI",
                "conclusion": "success",
                "task_id": "t-feed-notify",
            },
            headers={"Authorization": "Bearer feed-key"},
        )
    assert response.status_code == 202
    message = broadcast.call_args.args[1]
    assert message.conversationId == conv_id
    assert message.taskId == "t-feed-notify"
    assert message.source == "github"


async def test_task_lifecycle_notifications_share_conversation_thread(client, monkeypatch):
    _, db_url = client
    insert_guild(db_url, "g-activity-lifecycle")
    conv_id = insert_conversation(db_url, "g-activity-lifecycle")
    insert_task(db_url, "g-activity-lifecycle", "t-feed-lifecycle", conversation_id=conv_id)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    with (
        patch.object(discord_notifier, "notify_conversation_activity", AsyncMock()) as mirror,
        patch.object(discord_notifier, "archive_thread", AsyncMock()) as archive,
    ):
        await discord_notifier.notify_event(
            "task-complete", "Done", "Ready", task_id="t-feed-lifecycle", close=True
        )
        await discord_notifier.notify_existing_thread(
            "followup", "Follow-up", "Fix lint", task_id="t-feed-lifecycle"
        )
        await discord_notifier.notify_task_stream_event(
            "g-activity-lifecycle", "t-feed-lifecycle", "assigned", "Assigned", "Starting"
        )
        assert mirror.await_count == 3
        assert all(
            call.args[:2] == ("g-activity-lifecycle", conv_id) for call in mirror.call_args_list
        )
        archive.assert_not_awaited()


async def test_discord_stream_uses_conversation_without_opt_in(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.delenv("DISCORD_STREAM_TASKS", raising=False)
    with (
        patch.object(
            discord_notifier, "_lookup_foreman_thread_for_user", AsyncMock(return_value="thread-42")
        ) as lookup,
        patch.object(discord_notifier, "_bot_request", AsyncMock()) as post,
        patch.object(discord_notifier, "_ensure_task_thread", AsyncMock()) as legacy,
    ):
        try:
            await discord_notifier.notify_task_stream(
                "guild", "t-conv-stream", "hello @everyone", conversation_id=42
            )
            await discord_notifier.flush_task_stream("t-conv-stream")
            lookup.assert_awaited_once_with("guild", None, conversation_id=42)
            legacy.assert_not_awaited()
            assert post.call_args.args[1] == "/channels/thread-42/messages"
            assert post.call_args.args[2]["allowed_mentions"] == {"parse": []}
            assert "t-conv-stream" in post.call_args.args[2]["content"]
        finally:
            await discord_notifier.flush_task_stream("t-conv-stream")


async def test_discord_activity_chunks_without_mentions_or_fallback(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    with (
        patch.object(
            discord_notifier, "_lookup_foreman_thread_for_user", AsyncMock(return_value="thread-ci")
        ) as lookup,
        patch.object(discord_notifier, "_bot_request", AsyncMock()) as post,
    ):
        await discord_notifier.notify_conversation_activity("guild", 42, "@everyone " + "x" * 4500)
        assert post.await_count == 3
        assert all(len(call.args[2]["content"]) <= 2000 for call in post.call_args_list)
        assert all(
            call.args[2]["allowed_mentions"] == {"parse": []} for call in post.call_args_list
        )
        lookup.return_value = None
        post.reset_mock()
        await discord_notifier.notify_conversation_activity("guild", 42, "secret report")
        post.assert_not_awaited()
