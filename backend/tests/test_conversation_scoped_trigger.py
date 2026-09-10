"""Tests for the explicit `conversation_id` override added to the Foreman
trigger pipeline for #1297 (conversation-scoped message endpoint).

A (guild, user) pair can have more than one open ``Conversation`` (#1296), so
``routes.conversations.post_conversation_message`` needs to pin a run to the
exact conversation the caller posted into, rather than letting
``trigger_foreman``/``run_foreman_ai`` resolve "the user's current
conversation" the way every other human trigger path does. These tests cover
that plumbing without touching the real LLM loop (``_run_foreman_ai`` is
patched out, matching ``test_foreman_child_context.py``'s style).
"""

from __future__ import annotations

import foreman.triggers as triggers


async def test_trigger_foreman_with_conversation_id_skips_thread_ensure(monkeypatch):
    """An explicit conversation_id must not trigger the (guild, user)
    get-or-create-thread lookup — that could resolve a *different*
    conversation than the one the caller already picked."""
    ensure_called = {"value": False}

    async def fake_ensure(*a, **k):
        ensure_called["value"] = True
        return None

    async def fake_run_foreman_ai(*a, **k):
        return None

    monkeypatch.setattr(triggers, "ensure_conversation_thread", fake_ensure)
    monkeypatch.setattr(triggers, "run_foreman_ai", fake_run_foreman_ai)

    await triggers.trigger_foreman(
        "g1", "conversation-message", "hi", user_id="u-1", conversation_id=42
    )

    assert ensure_called["value"] is False


async def test_trigger_foreman_without_conversation_id_still_ensures_thread(monkeypatch):
    """Sanity check: existing chat behaviour (no task_id, no conversation_id)
    is unchanged by the new parameter."""
    ensure_called = {"value": False}

    async def fake_ensure(*a, **k):
        ensure_called["value"] = True
        return None

    async def fake_run_foreman_ai(*a, **k):
        return None

    monkeypatch.setattr(triggers, "ensure_conversation_thread", fake_ensure)
    monkeypatch.setattr(triggers, "run_foreman_ai", fake_run_foreman_ai)

    await triggers.trigger_foreman("g1", "chat", "hi", user_id="u-1")

    assert ensure_called["value"] is True


async def test_trigger_foreman_passes_conversation_id_through(monkeypatch):
    captured = {}

    def fake_spawn(coro, name=None):
        coro.close()
        return None

    def capturing_run(guild_id, human_message, **kwargs):
        captured["kwargs"] = kwargs

        async def _coro():
            return None

        return _coro()

    monkeypatch.setattr(triggers, "spawn", fake_spawn)
    monkeypatch.setattr(triggers, "run_foreman_ai", capturing_run)

    await triggers.trigger_foreman(
        "g1", "conversation-message", "hi", user_id="u-1", conversation_id=42
    )

    assert captured["kwargs"]["conversation_id"] == 42
    assert captured["kwargs"]["task_id"] is None


async def test_run_foreman_ai_forwards_conversation_id_to_impl(monkeypatch):
    import foreman.runner as runner

    captured = {}

    async def fake_run(*a, **kwargs):
        captured["kwargs"] = kwargs

    monkeypatch.setattr(runner, "_run_foreman_ai", fake_run)

    await runner.run_foreman_ai("g1", "hi", user_id="u-1", is_human=True, conversation_id=42)

    assert captured["kwargs"]["conversation_id"] == 42


def test_is_human_event_includes_conversation_message():
    from foreman.classify import is_human_event

    assert is_human_event("conversation-message") is True


def test_format_conversation_message_mentions_conversation_and_content():
    text = triggers.format_conversation_message(42, "please keep going")
    assert "42" in text
    assert "please keep going" in text
    assert "send_followup" in text
