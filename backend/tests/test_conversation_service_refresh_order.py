"""Conversation mutators must refresh from the DB before broadcasting (#1349)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from foreman import conversation_service as svc
from models import Conversation


def _recording_db(calls):
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock(side_effect=lambda: calls.append("commit"))
    db.refresh = AsyncMock(side_effect=lambda *_: calls.append("refresh"))
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        lambda db, c: svc.reactivate_conversation(db, c),
        lambda db, c: svc.set_conversation_status(db, c, "archived"),
        lambda db, c: svc.rename_conversation(db, c, "new name"),
        lambda db, c: svc.close_conversation(db, c),
    ],
    ids=["reactivate", "set_status", "rename", "close"],
)
async def test_refresh_precedes_broadcast(call):
    calls: list[str] = []
    db = _recording_db(calls)
    conv = Conversation(id=1, guild_id=1, user_id="u", status="archived")
    with (
        patch.object(
            svc,
            "broadcast_conversation_updated",
            new=AsyncMock(side_effect=lambda *_: calls.append("broadcast")),
        ),
        patch.object(svc, "_mirror_status_to_discord", new=AsyncMock()),
    ):
        await call(db, conv)
    assert calls == ["commit", "refresh", "broadcast"]
