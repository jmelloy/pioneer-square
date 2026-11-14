"""Regression coverage for inline images bloating conversation history."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import artifact_service
import pytest
from message_artifacts import externalize_images, message_payloads
from models import Message

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 1000000
DATA = base64.b64encode(PNG).decode()
IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": DATA}}


@pytest.fixture
def storage(monkeypatch):
    store = AsyncMock(
        return_value=artifact_service.StoredObject(
            "image-id",
            "artifacts/image.png",
            "https://example.com/image.png",
            "image/png",
            len(PNG),
        )
    )
    monkeypatch.setattr(artifact_service, "store", store)
    return store


async def test_claude_image_summary_and_detail_become_links(storage):
    db = MagicMock()
    output = json.dumps([{"type": "text", "text": "Screenshot"}, IMAGE])
    payload, complete = await externalize_images(
        {"line": f"  → {output}", "detail": {"toolType": "tool_result", "output": output}},
        db,
        "guild",
        1,
        "t-1",
    )
    assert complete
    assert DATA not in json.dumps(payload)
    assert len(json.dumps(payload)) < 2000
    assert "artifact://image-id" in payload["line"]
    assert "Screenshot" in payload["detail"]["output"]
    storage.assert_awaited_once_with(PNG, "image/png", "guild", "t-1")
    artifact = db.add.call_args.args[0]
    assert artifact.guild_id == 1
    assert artifact.created_at is not None


async def test_pi_structured_images_and_data_urls_become_links(storage):
    payload, complete = await externalize_images(
        {
            "event": {"content": [{"type": "image", "data": DATA, "mimeType": "image/png"}]},
            "text": f"![shot](data:image/png;base64,{DATA})",
        },
        MagicMock(),
        "guild",
        1,
        None,
    )
    assert complete
    assert DATA not in json.dumps(payload)
    assert payload["text"] == "[image](artifact://image-id)"
    storage.assert_awaited_once()


async def test_history_migrates_once_and_keeps_image_out_of_raw_meta(storage):
    db = MagicMock()
    db.commit = AsyncMock()
    message = Message(
        id=1,
        guild_id=1,
        content="Screenshot",
        from_agent="worker",
        to_agent="user",
        meta=json.dumps({"detail": {"output": json.dumps([IMAGE])}}),
    )
    result = await message_payloads([message], db, "guild", 1)
    assert DATA not in json.dumps(result)
    assert "artifact://image-id" in message.meta
    await message_payloads([message], db, "guild", 1)
    storage.assert_awaited_once()


async def test_storage_failure_preserves_historical_bytes(storage):
    storage.side_effect = artifact_service.ArtifactError("Storage unavailable", 503)
    db = MagicMock()
    db.commit = AsyncMock()
    original = json.dumps([IMAGE])
    message = Message(id=1, guild_id=1, content=original, from_agent="worker", to_agent="user")
    result = await message_payloads([message], db, "guild", 1)
    assert DATA not in json.dumps(result)
    assert "image unavailable" in result[0]["content"]
    assert message.content == original


@pytest.mark.parametrize("data", ["not valid base64!", "A" * (14 * 1024 * 1024)])
async def test_invalid_and_oversized_images_do_not_reach_storage(storage, data):
    payload, complete = await externalize_images(
        {"image": {"type": "image", "data": data, "mimeType": "image/png"}},
        MagicMock(),
        "guild",
        1,
        None,
    )
    assert not complete
    assert payload["image"]["text"] == "[image unavailable]"
    storage.assert_not_awaited()


async def test_terminal_handler_persists_and_broadcasts_links(storage, monkeypatch):
    import ws_handlers
    from ws_types import TerminalOutputMsg

    broadcast = AsyncMock()
    monkeypatch.setattr(ws_handlers, "broadcast_msg", broadcast)
    db = MagicMock()
    db.commit = AsyncMock()
    ctx = SimpleNamespace(db=db, guild_id="guild", guild_pk=1)
    await ws_handlers.handle_terminal_output(
        ctx,
        TerminalOutputMsg(
            workerId="w-1",
            line=json.dumps([IMAGE]),
            detail={"output": json.dumps([IMAGE])},
            level="debug",
        ),
    )
    log = db.add.call_args.args[0]
    assert DATA not in log.line
    assert DATA not in log.data
    sent = broadcast.call_args.args[1]
    assert "artifact://image-id" in sent.line
    assert DATA not in sent.model_dump_json()
    db.commit.assert_awaited_once()


async def test_conversation_route_returns_links_for_legacy_images(storage, monkeypatch):
    from routes import conversations

    monkeypatch.setattr(
        conversations,
        "_get_conversation_in_guild",
        AsyncMock(return_value=SimpleNamespace(id=1, guild_id=1)),
    )
    db = MagicMock()
    db.commit = AsyncMock()
    message = Message(id=1, guild_id=1, content=json.dumps([IMAGE]))
    result = MagicMock()
    result.all.return_value = [message]
    db.exec = AsyncMock(return_value=result)
    payload = await conversations.list_conversation_messages("guild", 1, "user", db)
    assert DATA not in json.dumps(payload)
    assert "artifact://image-id" in payload[0]["content"]
    assert "artifact://image-id" in message.content


async def test_plain_text_is_unchanged(storage):
    original = {"line": "Hello", "detail": {"output": "normal text"}}
    payload, complete = await externalize_images(original, MagicMock(), "guild", 1, None)
    assert complete
    assert payload == original
    storage.assert_not_awaited()
