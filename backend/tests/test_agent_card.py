"""Tests for the A2A AgentCard endpoint and serialisation."""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import insert_guild, make_auth_token

# ---------------------------------------------------------------------------
# AgentCard model unit tests
# ---------------------------------------------------------------------------


def test_agent_card_model_serialisation():
    """AgentCard serialises to camelCase JSON matching the A2A spec."""
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from routes.wellknown import AgentCapabilities, AgentCard, AgentProvider, AgentSkill

    card = AgentCard(
        name="Test Agent",
        description="A test agent",
        url="https://example.com",
        version="1.2.3",
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="skill-1",
                name="Skill One",
                description="Does something",
                tags=["coding"],
            )
        ],
        provider=AgentProvider(organization="ACME", url="https://acme.example.com"),
    )

    data = card.model_dump(by_alias=True, exclude_none=True)

    # Required top-level camelCase fields
    assert data["name"] == "Test Agent"
    assert data["description"] == "A test agent"
    assert data["url"] == "https://example.com"
    assert data["version"] == "1.2.3"
    assert data["defaultInputModes"] == ["text/plain"]
    assert data["defaultOutputModes"] == ["text/plain"]

    # Nested capabilities (camelCase)
    caps = data["capabilities"]
    assert caps["streaming"] is True
    assert caps["pushNotifications"] is False

    # Skills
    skill = data["skills"][0]
    assert skill["id"] == "skill-1"
    assert skill["name"] == "Skill One"
    assert skill["tags"] == ["coding"]

    # Provider
    assert data["provider"]["organization"] == "ACME"
    assert data["provider"]["url"] == "https://acme.example.com"


def test_agent_card_no_snake_case_keys():
    """Output JSON must not contain snake_case keys."""
    from routes.wellknown import AgentCapabilities, AgentCard, AgentSkill

    card = AgentCard(
        name="X",
        description="Y",
        url="https://x.example.com",
        version="0.1",
        capabilities=AgentCapabilities(),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[],
    )
    data = card.model_dump(by_alias=True, exclude_none=True)
    raw = json.dumps(data)
    assert "default_input_modes" not in raw
    assert "default_output_modes" not in raw
    assert "push_notifications" not in raw
    assert "defaultInputModes" in raw
    assert "defaultOutputModes" in raw


# ---------------------------------------------------------------------------
# Endpoint integration tests
# ---------------------------------------------------------------------------


def test_agent_card_endpoint_no_subdomain(client):
    """/.well-known/agent.json returns a valid card even with no guild context."""
    test_client, _ = client
    resp = test_client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")

    data = resp.json()
    assert "name" in data
    assert "description" in data
    assert "url" in data
    assert "version" in data
    assert "capabilities" in data
    assert "defaultInputModes" in data
    assert "defaultOutputModes" in data
    assert isinstance(data["skills"], list)
    # Foreman skill is always present
    assert any(s["id"] == "foreman" for s in data["skills"])


def test_guild_agent_card_endpoint_authenticated(client):
    """GET /guilds/{id}/agent-card returns card with guild fields."""
    test_client, db_url = client
    insert_guild(db_url, "gld001", name="My Factory")
    token = make_auth_token(db_url)

    resp = test_client.get(
        "/guilds/gld001/agent-card",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "My Factory"
    assert "foreman" in [s["id"] for s in data["skills"]]


def test_guild_agent_card_requires_auth(client):
    """GET /guilds/{id}/agent-card is auth-gated."""
    test_client, db_url = client
    insert_guild(db_url, "gld002", name="Private Guild")

    resp = test_client.get("/guilds/gld002/agent-card")
    assert resp.status_code == 401


def test_guild_agent_card_not_found(client):
    """GET /guilds/{id}/agent-card returns 404 for unknown guild."""
    test_client, db_url = client
    token = make_auth_token(db_url)

    resp = test_client.get(
        "/guilds/nonexistent/agent-card",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (403, 404)


def test_guild_update_a2a_fields(client):
    """PATCH /guilds/{id} persists description/url/version."""
    test_client, db_url = client
    insert_guild(db_url, "gld003", name="Factory")
    token = make_auth_token(db_url)

    resp = test_client.patch(
        "/guilds/gld003",
        json={
            "description": "A steampunk factory floor",
            "url": "https://factory.example.com",
            "version": "2.0.0",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["description"] == "A steampunk factory floor"
    assert data["url"] == "https://factory.example.com"
    assert data["version"] == "2.0.0"

    # Verify the agent card reflects the updated fields
    resp2 = test_client.get(
        "/guilds/gld003/agent-card",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 200
    card = resp2.json()
    assert card["description"] == "A steampunk factory floor"
    assert card["url"] == "https://factory.example.com"
    assert card["version"] == "2.0.0"
