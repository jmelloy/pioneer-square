"""Tests for worker name: stored from registration response, droid-style format."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pioneer_worker.config import Config
from pioneer_worker.worker import Worker


def _make_worker(cfg: Config | None = None) -> Worker:
    if cfg is None:
        cfg = Config(backend_url="ws://localhost:8000", guild_id="testguild", max_agents=1)
    return Worker(cfg)


# ---------------------------------------------------------------------------
# _worker_name initialised to empty string before registration
# ---------------------------------------------------------------------------


def test_worker_name_default_is_empty():
    w = _make_worker()
    assert w._worker_name == ""


# ---------------------------------------------------------------------------
# _register stores name from response
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_register_stores_name_from_response():
    cfg = Config(backend_url="ws://localhost:8000", guild_id="g1", max_agents=1)
    w = _make_worker(cfg)

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "id": "w-abc123",
        "name": "ABC-123",
        "auth_token": "secret-token",
    }

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch.object(w, "_http", return_value=mock_client):
        await w._register()

    assert w._worker_name == "ABC-123"
    assert w.cfg.worker_id == "w-abc123"


@pytest.mark.anyio
async def test_register_name_falls_back_to_worker_id_when_absent():
    """When backend omits the ``name`` field the worker_id is used as fallback."""
    cfg = Config(backend_url="ws://localhost:8000", guild_id="g1", max_agents=1)
    w = _make_worker(cfg)

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "id": "w-xyz999",
        "auth_token": "token",
        # no "name" field (older backend)
    }

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch.object(w, "_http", return_value=mock_client):
        await w._register()

    assert w._worker_name == "w-xyz999"


# ---------------------------------------------------------------------------
# Name format invariants (droid-style: leading-letters + '-' + rest, uppercased)
# ---------------------------------------------------------------------------


def _droid_format(worker_id: str) -> str:
    bare = worker_id.removeprefix("w-")
    m = re.search(r"\d", bare)
    if m and m.start() > 0:
        return f"{bare[: m.start()].upper()}-{bare[m.start() :].upper()}"
    return bare.upper()


@pytest.mark.parametrize(
    "worker_id, expected",
    [
        ("w-vd3566", "VD-3566"),
        ("w-ab1234", "AB-1234"),
        ("w-x9", "X-9"),
        ("w-abc123", "ABC-123"),
        ("w-g2otus", "G-2OTUS"),
    ],
)
def test_name_format(worker_id, expected):
    """Droid-style: leading letters uppercased, '-', then rest uppercased."""
    assert _droid_format(worker_id) == expected
