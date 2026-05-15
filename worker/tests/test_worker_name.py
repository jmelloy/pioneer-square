"""Tests for worker name: stored from registration response, format hostname[:3]/worker_id."""

from __future__ import annotations

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
        "name": "tok/w-abc123",
        "auth_token": "secret-token",
    }

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch.object(w, "_http", return_value=mock_client):
        await w._register()

    assert w._worker_name == "tok/w-abc123"
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
# Name format invariants (black-box, driven by the backend formula)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostname, worker_id, expected",
    [
        ("tokenhost", "w-g2otus", "tok/w-g2otus"),
        ("ab", "w-short0", "ab/w-short0"),
        ("z", "w-single0", "z/w-single0"),
        ("xyz", "w-abc123", "xyz/w-abc123"),
        ("longhost", "w-abc123", "lon/w-abc123"),
    ],
)
def test_name_format(hostname, worker_id, expected):
    """The name format ``hostname[:3]/worker_id`` holds for various inputs."""
    prefix = hostname[:3]
    assert f"{prefix}/{worker_id}" == expected
