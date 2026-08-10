"""Proves the PR-review metadata handoff (issue #1124 / PR #1125) actually
reaches ``assign_task`` as structured fields, not just as text in the
Foreman's instructions.

PR #1125's fix embeds ``pr_repo``/``pr_number``/``branch``/``pr_url``/
``head_sha`` into the instructional text sent to the Foreman LLM, and teaches
``assign_task`` to accept and persist those fields *if* the Foreman passes
them.  Neither half, alone, proves the other: text containing the right
values doesn't guarantee the LLM copies them into a tool call, and a tool
handler that persists whatever it's given doesn't guarantee anything was
handed to it.  The worker-side regression test (worker/tests/
test_review_phase_guard.py::test_review_task_1758_regression_resolves_branch_from_metadata)
proves the worker fails fast on missing metadata — it does not prove the
metadata ever arrives.

This test closes that gap for the backend half of the pipeline, using a
#1758-style (Identity-Digital/dnsid) webhook payload:

1. Feeds a realistic ``pull_request``/``review_requested`` payload through the
   real ``_build_foreman_summary`` and confirms it embeds the true PR
   coordinates as an explicit assign_task instruction (the text half).
2. Independently derives those same coordinates from the payload and drives
   ``exec_tools`` with an ``assign_task`` call carrying them — simulating a
   compliant Foreman turn — while spying on ``foreman.tools.broadcast`` to
   capture exactly what assign_task hands off (the structured half).
3. Asserts ``pr_repo``, ``pr_number``, ``branch``, ``pr_url``, and
   ``head_sha`` are all non-null in the broadcast TaskAssignedMsg, and that
   ``pr_repo``, ``pr_number``, ``branch``, ``pr_url`` are persisted non-null
   on the Task row (``head_sha`` is informational-only and not a DB column).
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module
from _test_config import TEST_DATABASE_URL
from foreman.tools import exec_tools
from helpers import create_db, insert_guild, insert_worker, truncate_all
from models import Task
from routes.webhooks import _build_foreman_summary

# ---------------------------------------------------------------------------
# #1758-style (Identity-Digital/dnsid) fixture — mirrors issue #1124's
# regression case verbatim.
# ---------------------------------------------------------------------------

PR_REPO = "Identity-Digital/dnsid"
PR_NUMBER = 1758
PR_BRANCH = "wolfgang/issue-1481-provider-budget"
PR_URL = "https://github.com/Identity-Digital/dnsid/pull/1758"
PR_HEAD_SHA = "7c10afdc25388b3a59588a79cad075f640ce6611"


def _review_requested_payload() -> dict:
    return {
        "action": "review_requested",
        "pull_request": {
            "number": PR_NUMBER,
            "title": "Add provider budget support",
            "html_url": PR_URL,
            "head": {"ref": PR_BRANCH, "sha": PR_HEAD_SHA},
        },
        "requested_reviewer": {"login": "wolfgang"},
        "repository": {"full_name": PR_REPO},
    }


@pytest.fixture()
def db_session(monkeypatch):
    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)

    yield TEST_DATABASE_URL


def _tool(name: str, inputs: dict, tool_id: str = "tool-handoff") -> SimpleNamespace:
    return SimpleNamespace(name=name, input=inputs, id=tool_id)


async def _get_task(task_id: str) -> Task | None:
    async with database_module.AsyncSessionLocal() as db:
        return (await db.exec(select(Task).where(col(Task.id) == task_id))).one_or_none()


# ---------------------------------------------------------------------------
# Half 1 — the webhook handler must embed the true PR coordinates as an
# explicit, structured assign_task instruction (not just prose).
# ---------------------------------------------------------------------------


def test_webhook_summary_instructs_assign_task_with_pr_coordinates():
    payload = _review_requested_payload()
    summary = _build_foreman_summary(
        "pull_request", "review_requested", payload, PR_REPO, PR_NUMBER, "", "wolfgang"
    )

    assert f"pr_number={PR_NUMBER}" in summary
    assert f'pr_repo="{PR_REPO}"' in summary
    assert f'branch="{PR_BRANCH}"' in summary
    assert f'pr_url="{PR_URL}"' in summary
    assert f'head_sha="{PR_HEAD_SHA}"' in summary


# ---------------------------------------------------------------------------
# Half 2 — when assign_task is invoked with those coordinates (as the
# instructions above direct), the structured fields — not just text — must
# be received and stored, both in the broadcast to the worker and on the
# persisted Task row.
# ---------------------------------------------------------------------------


class TestAssignTaskReceivesStructuredPrMetadata:
    @pytest.mark.asyncio
    async def test_assign_task_broadcast_and_task_row_carry_non_null_pr_fields(self, db_session):
        guild_id = "g-handoff-1758"
        worker_id = "w-handoff-1758"
        insert_guild(db_session, guild_id)
        insert_worker(
            db_session,
            guild_id,
            worker_id,
            state="idle",
            tools='["claude"]',
            repos=f'["{PR_REPO}"]',
        )

        tu = _tool(
            "assign_task",
            {
                "worker_id": worker_id,
                "description": f"Review PR #{PR_NUMBER}",
                "phase": "review",
                "pr_number": PR_NUMBER,
                "pr_repo": PR_REPO,
                "branch": PR_BRANCH,
                "pr_url": PR_URL,
                "head_sha": PR_HEAD_SHA,
            },
        )

        with (
            patch("foreman.tools.broadcast", new=AsyncMock()) as mock_broadcast,
            patch("foreman.tools.emit_terminal_line", new=AsyncMock()),
        ):
            results = await exec_tools(guild_id, [tu])

        assert not results[0].get("is_error"), f"assign_task failed: {results[0]['content']}"
        content = results[0]["content"]
        task_id = next(tok for tok in content.split() if tok.startswith("t-"))

        # --- structured fields the worker's WS handler actually reads ---
        assert mock_broadcast.await_count == 1
        _, msg = mock_broadcast.await_args.args
        assert msg["prRepo"] == PR_REPO
        assert msg["prNumber"] == PR_NUMBER
        assert msg["branch"] == PR_BRANCH
        assert msg["prUrl"] == PR_URL
        assert msg["headSha"] == PR_HEAD_SHA
        for field in ("prRepo", "prNumber", "branch", "prUrl", "headSha"):
            assert msg[field] is not None, f"{field} must not be null in the assign_task broadcast"

        # --- persisted on the Task row (head_sha is informational-only,
        # not a DB column, so it is not asserted here) ---
        task = await _get_task(task_id)
        assert task is not None, "Task row not created"
        assert task.pr_repo == PR_REPO
        assert task.pr_number == PR_NUMBER
        assert task.branch == PR_BRANCH
        assert task.pr_url == PR_URL
        for field_name in ("pr_repo", "pr_number", "branch", "pr_url"):
            assert getattr(task, field_name) is not None, (
                f"Task.{field_name} must not be null after assign_task metadata handoff"
            )

    @pytest.mark.asyncio
    async def test_assign_task_without_pr_metadata_leaves_fields_null(self, db_session):
        """Sanity check for the assertions above: omitting the fields (the pre-fix
        behavior) must leave them null, proving the prior test isn't trivially
        satisfied by unconditional defaults somewhere in the handler."""
        guild_id = "g-handoff-nometa"
        worker_id = "w-handoff-nometa"
        insert_guild(db_session, guild_id)
        insert_worker(db_session, guild_id, worker_id, state="idle", tools='["claude"]')

        tu = _tool(
            "assign_task",
            {
                "worker_id": worker_id,
                "description": "Review PR",
                "phase": "review",
            },
        )

        with (
            patch("foreman.tools.broadcast", new=AsyncMock()),
            patch("foreman.tools.emit_terminal_line", new=AsyncMock()),
        ):
            results = await exec_tools(guild_id, [tu])

        assert not results[0].get("is_error")
        content = results[0]["content"]
        task_id = next(tok for tok in content.split() if tok.startswith("t-"))

        task = await _get_task(task_id)
        assert task is not None
        assert task.pr_repo is None
        assert task.pr_number is None
        assert task.branch is None
        assert task.pr_url is None
