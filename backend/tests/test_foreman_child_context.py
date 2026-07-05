"""Tests for the embedded foreman's per-task child context wiring (issue #649).

Covers the feature gate, the per-task lock-key selection in run_foreman_ai, and
the child-event routing in ws_handlers._trigger_foreman. The actual Claude turn
(_run_foreman_ai) is patched out — these exercise the routing/serialization
plumbing, not the LLM loop.
"""

from __future__ import annotations

import asyncio

import pytest

# ── feature gate ─────────────────────────────────────────────────────────────


def test_child_contexts_enabled_default_on(monkeypatch):
    import foreman.runner as runner

    monkeypatch.delenv("FOREMAN_CHILD_CONTEXTS", raising=False)
    assert runner._child_contexts_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_child_contexts_disabled_values(monkeypatch, val):
    import foreman.runner as runner

    monkeypatch.setenv("FOREMAN_CHILD_CONTEXTS", val)
    assert runner._child_contexts_enabled() is False


def test_child_contexts_enabled_truthy(monkeypatch):
    import foreman.runner as runner

    monkeypatch.setenv("FOREMAN_CHILD_CONTEXTS", "1")
    assert runner._child_contexts_enabled() is True


# ── per-task lock key in run_foreman_ai ──────────────────────────────────────


async def test_child_run_uses_per_task_lock_key(monkeypatch):
    """A child run keys its lock on (guild, task:<id>), not (guild, user)."""
    import foreman.runner as runner

    seen_keys = []

    async def fake_run(*a, **k):
        # Snapshot the lock keys present while the run holds its lock.
        seen_keys.extend(list(runner._guild_locks.keys()))

    monkeypatch.setattr(runner, "_run_foreman_ai", fake_run)
    monkeypatch.setattr(runner, "_child_contexts_enabled", lambda: True)

    await runner.run_foreman_ai("g1", "task-complete", user_id="u-1", task_id="t-abc", child=True)

    assert ("g1", "task:t-abc") in seen_keys
    assert ("g1", "u-1") not in seen_keys


async def test_parent_run_uses_guild_user_lock_key(monkeypatch):
    import foreman.runner as runner

    seen_keys = []

    async def fake_run(*a, **k):
        seen_keys.extend(list(runner._guild_locks.keys()))

    monkeypatch.setattr(runner, "_run_foreman_ai", fake_run)

    await runner.run_foreman_ai("g1", "hi", user_id="u-1")

    assert ("g1", "u-1") in seen_keys


async def test_child_gate_off_falls_back_to_parent_key(monkeypatch):
    import foreman.runner as runner

    seen_keys = []

    async def fake_run(*a, **k):
        seen_keys.extend(list(runner._guild_locks.keys()))
        # child must be coerced to False when the gate is off
        assert k.get("child") is False

    monkeypatch.setattr(runner, "_run_foreman_ai", fake_run)
    monkeypatch.setattr(runner, "_child_contexts_enabled", lambda: False)

    await runner.run_foreman_ai("g1", "task-complete", user_id="u-1", task_id="t-abc", child=True)

    assert ("g1", "u-1") in seen_keys
    assert ("g1", "task:t-abc") not in seen_keys


async def test_different_tasks_run_concurrently(monkeypatch):
    """Two child runs for different tasks must not block each other."""
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
    monkeypatch.setattr(runner, "_child_contexts_enabled", lambda: True)

    t1 = asyncio.create_task(
        runner.run_foreman_ai("g1", "x", user_id="u", task_id="t-1", child=True)
    )
    t2 = asyncio.create_task(
        runner.run_foreman_ai("g1", "y", user_id="u", task_id="t-2", child=True)
    )
    await asyncio.sleep(0.01)
    release.set()
    await asyncio.gather(t1, t2)

    assert overlap["value"] is True


async def test_same_task_drops_concurrent_run(monkeypatch):
    """A second child run for the same task while one is in-flight is dropped."""
    import foreman.runner as runner

    runner._guild_locks.pop(("g1", "task:t-dup"), None)
    calls = 0
    release = asyncio.Event()

    async def fake_run(*a, **k):
        nonlocal calls
        calls += 1
        await release.wait()

    monkeypatch.setattr(runner, "_run_foreman_ai", fake_run)
    monkeypatch.setattr(runner, "_child_contexts_enabled", lambda: True)

    t1 = asyncio.create_task(
        runner.run_foreman_ai("g1", "x", user_id="u", task_id="t-dup", child=True)
    )
    await asyncio.sleep(0.01)  # let t1 acquire the lock
    # Second invocation should see the lock held and drop immediately.
    await runner.run_foreman_ai("g1", "x2", user_id="u", task_id="t-dup", child=True)
    release.set()
    await t1

    assert calls == 1


# ── _trigger_foreman child-event routing ─────────────────────────────────────


@pytest.mark.parametrize(
    "event,expect_child",
    [
        ("task-complete", True),
        ("followup-done", True),
        ("needs-input", True),
        ("task-error", True),
        ("chat", False),
        ("worker-online", False),
        ("periodic-check", False),
        ("claude-auth", False),
    ],
)
async def test_trigger_foreman_child_routing(monkeypatch, event, expect_child):
    import ws_handlers

    captured = {}

    def fake_spawn(coro, name=None):
        # Drain the coroutine so there's no "never awaited" warning.
        coro.close()
        return None

    def capturing_run(guild_id, human_message, **kwargs):
        captured["kwargs"] = kwargs
        captured["guild_id"] = guild_id

        async def _coro():
            return None

        return _coro()

    # No external foreman connected → embedded fallback path.
    monkeypatch.setattr(ws_handlers, "foreman_connections", {})
    monkeypatch.setattr(ws_handlers, "spawn", fake_spawn)
    monkeypatch.setattr(ws_handlers, "run_foreman_ai", capturing_run)

    await ws_handlers._trigger_foreman(
        "g1", event, "msg", user_id="u-1", task_id="t-xyz" if expect_child else None
    )

    assert captured["kwargs"].get("child") is expect_child
    if expect_child:
        assert captured["kwargs"].get("task_id") == "t-xyz"


# ── _emit_foreman_chat: frontend badge vs Discord routing ────────────────────


def _patch_emit(monkeypatch):
    """Patch broadcast_msg / discord mirror / spawn on foreman.runner.

    Returns ``(sent_msgs, discord_calls)`` — the ChatMsg objects broadcast to
    WS clients and the ``(content, task_id)`` tuples passed to the Discord
    mirror. ``spawn`` is replaced with a version that actually awaits the
    coroutine so the Discord call is observed.
    """
    import foreman.runner as runner

    sent_msgs: list = []
    discord_calls: list = []

    async def fake_broadcast(guild_id, msg):
        sent_msgs.append(msg)

    async def fake_notify(guild_id, content, task_id=None):
        discord_calls.append((content, task_id))

    def fake_spawn(coro, name=None):
        return asyncio.ensure_future(coro)

    monkeypatch.setattr(runner, "broadcast_msg", fake_broadcast)
    monkeypatch.setattr(runner.discord_notifier, "notify_foreman_chat", fake_notify)
    monkeypatch.setattr(runner, "spawn", fake_spawn)
    return sent_msgs, discord_calls


async def test_emit_child_run_badges_and_routes_to_task_thread(monkeypatch):
    """A child run tags the WS message and mirrors to the task's Discord thread."""
    import foreman.runner as runner

    sent_msgs, discord_calls = _patch_emit(monkeypatch)

    await runner._emit_foreman_chat(
        "g1",
        "working on it",
        "2026-01-01T00:00:00Z",
        child_task_id="t-abc",
        discord_task_id="t-abc",
    )
    await asyncio.sleep(0)  # let the spawned Discord mirror run

    assert sent_msgs[0].taskId == "t-abc"
    assert discord_calls == [("working on it", "t-abc")]


async def test_emit_parent_thread_chat_routes_to_thread_without_badge(monkeypatch):
    """A parent run answering a task-thread message still replies in that thread.

    The frontend badge (``taskId`` on the WS ChatMsg) stays absent — the run is
    the parent/whole-guild conversation — but the Discord mirror routes to the
    task's thread so the reply lands where the human is talking.
    """
    import foreman.runner as runner

    sent_msgs, discord_calls = _patch_emit(monkeypatch)

    await runner._emit_foreman_chat(
        "g1", "here you go", "2026-01-01T00:00:00Z", child_task_id=None, discord_task_id="t-abc"
    )
    await asyncio.sleep(0)

    assert sent_msgs[0].taskId is None
    assert discord_calls == [("here you go", "t-abc")]


async def test_emit_plain_parent_run_has_no_task_routing(monkeypatch):
    """A parent run with no task concern badges nothing and posts to main channel."""
    import foreman.runner as runner

    sent_msgs, discord_calls = _patch_emit(monkeypatch)

    await runner._emit_foreman_chat(
        "g1", "guild status", "2026-01-01T00:00:00Z", child_task_id=None, discord_task_id=None
    )
    await asyncio.sleep(0)

    assert sent_msgs[0].taskId is None
    assert discord_calls == [("guild status", None)]
