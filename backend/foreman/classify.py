"""Shared classification of foreman trigger events by origin (human vs. automated).

Human-originated triggers (Discord/web UI chat, user-requested follow-ups) are
queued rather than dropped when the foreman is busy — see
``foreman.runner.run_foreman_ai``'s ``is_human`` param. Two call sites need to
agree on this classification, so it lives here as a single source of truth:

- ``foreman.triggers.trigger_foreman`` — event-driven dispatch (websocket-origin
  triggers: ``chat``, ``task-complete``, ``periodic-check``, etc).
- ``routes.tasks.create_task_followup`` — the REST follow-up endpoint, which
  has no dispatch ``event`` string of its own but tags its call with the
  synthetic event name ``"user-followup"`` purely to share this classifier.

If you add a new human-originated trigger path, add its event name to
``_HUMAN_FOREMAN_EVENTS`` below and route it through ``is_human_event`` rather
than hand-rolling the check.
"""

_HUMAN_FOREMAN_EVENTS = frozenset({"chat", "user-followup"})


def is_human_event(event: str) -> bool:
    """Return True if *event* originates from a human, not an automated trigger."""
    return event in _HUMAN_FOREMAN_EVENTS
