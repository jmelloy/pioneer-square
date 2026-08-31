"""Tests for the embedded foreman's unified conversation context (issue #1200).

Issue #1200 removed the per-task child Foreman context introduced by #649:
every run for a (guild, user) now shares one lock and one ``ForemanTurn``
history, and ``task_id`` is metadata on a turn rather than a separate context
selector. These tests cover the lock-key unification in ``run_foreman_ai``,
the (now child-free) trigger routing in ``foreman.triggers.trigger_foreman``,
and ``_emit_foreman_chat``'s badge-vs-Discord-routing behaviour. The actual
Claude turn (``_run_foreman_ai``) is patched out — these exercise routing/
serialization plumbing, not the LLM loop.
"""

from __future__ import annotations

import asyncio

import pytest

# ── lock key in run_foreman_ai ───────────────────────────────────────────────


async def test_run_uses_guild_user_lock_key_regardless_of_task_id(monkeypatch):
    """Every run keys its lock on (guild, user), whether or not task_id is set."""
    import foreman.runner as runner

    seen_keys = []

    async def fake_run(*a, **k):
        seen_keys.extend(list(runner._guild_locks.keys()))

    monkeypatch.setattr(runner, "_run_foreman_ai", fake_run)

    await runner.run_foreman_ai("g1", "task-complete", user_id="u-1", task_id="t-abc")

    assert ("g1", "u-1") in seen_keys
    assert ("g1", "task:t-abc") not in seen_keys


async def test_task_event_and_chat_share_the_same_lock(monkeypatch):
    """A task-triggered event and that user's own chat serialise against each
    other — they share the (guild, user) lock, not independent contexts."""
    import foreman.runner as runner

    started = asyncio.Event()
    release = asyncio.Event()
    overlap = {"value": False}
    active = 0

    async def fake_run(*a, **k):
        nonlocal active
        active += 1
        if active > 1:
            overlap["value"] = True
        started.set()
        await release.wait()
        active -= 1

    monkeypatch.setattr(runner, "_run_foreman_ai", fake_run)

    t1 = asyncio.create_task(
        runner.run_foreman_ai("g1", "task-complete", user_id="u", task_id="t-1")
    )
    await asyncio.sleep(0.01)
    # Same user's chat while the task event is in flight must be dropped
    # (automated invocation, non-human) rather than run concurrently.
    t2 = asyncio.create_task(runner.run_foreman_ai("g1", "hello", user_id="u"))
    await asyncio.sleep(0.01)
    release.set()
    await asyncio.gather(t1, t2)

    assert overlap["value"] is False


async def test_different_users_run_concurrently(monkeypatch):
    """Two runs for different users must not block each other."""
    import foreman.runner as runner

    release = asyncio.Event()
    overlap = {"value": False}
    active = 0

    async def fake_run(*a, **k):
        nonlocal active
        active += 1
        if active > 1:
            overlap["value"] = True
        await release.wait()
        active -= 1

    monkeypatch.setattr(runner, "_run_foreman_ai", fake_run)

    t1 = asyncio.create_task(runner.run_foreman_ai("g1", "x", user_id="u-1", task_id="t-1"))
    t2 = asyncio.create_task(runner.run_foreman_ai("g1", "y", user_id="u-2", task_id="t-2"))
    await asyncio.sleep(0.01)
    release.set()
    await asyncio.gather(t1, t2)

    assert overlap["value"] is True


# ── trigger_foreman routing ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "event",
    ["task-complete", "followup-done", "needs-input", "task-error", "chat", "worker-online"],
)
async def test_trigger_foreman_never_passes_child(monkeypatch, event):
    """No trigger event selects a separate child context anymore — `child`
    must not appear in the kwargs run_foreman_ai is called with."""
    import foreman.triggers as triggers

    captured = {}

    def fake_spawn(coro, name=None):
        coro.close()
        return None

    def capturing_run(guild_id, human_message, **kwargs):
        captured["kwargs"] = kwargs
        captured["guild_id"] = guild_id

        async def _coro():
            return None

        return _coro()

    monkeypatch.setattr(triggers, "spawn", fake_spawn)
    monkeypatch.setattr(triggers, "run_foreman_ai", capturing_run)

    await triggers.trigger_foreman("g1", event, "msg", user_id="u-1", task_id="t-xyz")

    assert "child" not in captured["kwargs"]
    assert captured["kwargs"].get("task_id") == "t-xyz"


async def test_trigger_foreman_runs_embedded_even_when_proxy_connected(monkeypatch):
    import foreman.triggers as triggers

    captured = {}

    def fake_spawn(coro, name=None):
        coro.close()
        captured["name"] = name
        return None

    def capturing_run(guild_id, human_message, **kwargs):
        captured["guild_id"] = guild_id
        captured["human_message"] = human_message
        captured["kwargs"] = kwargs

        async def _coro():
            return None

        return _coro()

    monkeypatch.setattr(triggers, "spawn", fake_spawn)
    monkeypatch.setattr(triggers, "run_foreman_ai", capturing_run)

    await triggers.trigger_foreman("g1", "chat", "hello", user_id="u-1")

    assert captured["guild_id"] == "g1"
    assert captured["human_message"] == "hello"
    assert captured["kwargs"]["user_id"] == "u-1"


# ── _load_history no longer scopes by task_id ────────────────────────────────


def test_load_history_signature_has_no_task_id_param():
    """_load_history loads the whole (guild, user) conversation; task-triggered
    turns share it rather than reading an isolated per-task slice."""
    import inspect

    import foreman.runner as runner

    params = inspect.signature(runner._load_history).parameters
    assert "task_id" not in params


# ── TurnJournal.text: frontend badge vs Discord routing ──────────────────────
#
# _emit_foreman_chat (which these tests used to drive directly) was folded
# into TurnJournal by issue #1241 — narration now goes through
# TurnJournal.text()/tool_use()/tool_result() instead of a standalone
# function. These tests port the same badge-vs-Discord-routing assertions to
# the new seam: a TurnJournal built from a ForemanReply, with fake Events/
# Scheduler ports instead of patching module globals.


class _FakeEvents:
    """Records broadcast_msg calls; broadcast()/emit_terminal_line() are no-ops."""

    def __init__(self) -> None:
        self.sent: list = []

    async def broadcast(self, guild_id, message):
        pass

    async def broadcast_msg(self, guild_id, message):
        self.sent.append(message)

    async def emit_terminal_line(self, guild_id, agent_id, line):
        pass


class _FakeScheduler:
    """Actually runs spawned coroutines (via ensure_future) so the Discord
    mirror call is observable in the same test, unlike a no-op scheduler."""

    def spawn(self, coro, *, name=None):
        return asyncio.ensure_future(coro)


class _FakeDB:
    def add(self, obj):
        pass

    async def commit(self):
        pass

    async def close(self):
        pass


def _build_journal(monkeypatch, *, task_id, discord_task_id, discord_channel_id=None):
    """Build a TurnJournal wired to fakes, and patch its Discord/DB side effects.

    Returns ``(journal, sent_msgs, discord_calls)`` — the ChatMsg objects
    broadcast to WS clients and the ``(content, task_id)`` tuples passed to
    the Discord mirror.
    """
    import foreman.journal as journal_mod
    from foreman.journal import ForemanReply, TurnJournal
    from foreman.ports import SystemClock

    discord_calls: list = []

    async def fake_notify(guild_id, content, task_id=None, channel_id=None, user_id=None):
        discord_calls.append((content, task_id))

    async def fake_get_db():
        return _FakeDB()

    monkeypatch.setattr(journal_mod, "get_db", fake_get_db)
    monkeypatch.setattr(journal_mod.discord_notifier, "notify_foreman_chat", fake_notify)

    events = _FakeEvents()
    journal = TurnJournal(
        guild_id="g1",
        guild_pk=1,
        user_id=None,
        reply=ForemanReply(
            guild_id="g1",
            user_id=None,
            task_id=task_id,
            thread_id=None,
            discord_task_id=discord_task_id,
            discord_channel_id=discord_channel_id,
        ),
        events=events,
        scheduler=_FakeScheduler(),
        clock=SystemClock(),
    )
    return journal, events.sent, discord_calls


async def test_emit_task_scoped_run_badges_and_routes_to_task_thread(monkeypatch):
    """A run concerning a task tags the WS message and mirrors to that task's
    Discord thread."""
    journal, sent_msgs, discord_calls = _build_journal(
        monkeypatch, task_id="t-abc", discord_task_id="t-abc"
    )

    await journal.text("working on it")
    await asyncio.sleep(0)  # let the spawned Discord mirror run

    assert sent_msgs[0].taskId == "t-abc"
    assert discord_calls == [("working on it", "t-abc")]


async def test_emit_routes_to_task_thread_without_badge_when_task_id_omitted(monkeypatch):
    """A caller can route the Discord mirror to a task's thread without
    stamping a task_id badge on the WS message (discord_task_id is
    independent of task_id)."""
    journal, sent_msgs, discord_calls = _build_journal(
        monkeypatch, task_id=None, discord_task_id="t-abc"
    )

    await journal.text("here you go")
    await asyncio.sleep(0)

    assert sent_msgs[0].taskId is None
    assert discord_calls == [("here you go", "t-abc")]


async def test_emit_plain_run_has_no_task_routing(monkeypatch):
    """A run with no task concern badges nothing and posts to the main channel."""
    journal, sent_msgs, discord_calls = _build_journal(
        monkeypatch, task_id=None, discord_task_id=None
    )

    await journal.text("guild status")
    await asyncio.sleep(0)

    assert sent_msgs[0].taskId is None
    assert discord_calls == [("guild status", None)]
