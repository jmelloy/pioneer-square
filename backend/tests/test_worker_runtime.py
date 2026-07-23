"""Tests for backend/worker_runtime.py — Docker vs ECS worker dispatch."""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Make backend importable from tests/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import worker_runtime  # noqa: E402

TASK_ARN = "arn:aws:ecs:us-east-1:123456789012:task/my-cluster/0123456789abcdef0123456789abcdef"


@pytest.fixture
def ecs_env(monkeypatch):
    monkeypatch.setenv("ECS_CLUSTER_NAME", "my-cluster")
    monkeypatch.setenv("ECS_WORKER_TASK_DEFINITION", "my-worker-taskdef")
    monkeypatch.setenv("ECS_WORKER_SUBNETS", "subnet-1,subnet-2")
    monkeypatch.setenv("ECS_WORKER_SECURITY_GROUPS", "sg-1")


# ---------------------------------------------------------------------------
# Runtime selection
# ---------------------------------------------------------------------------


def test_ecs_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ECS_CLUSTER_NAME", raising=False)
    monkeypatch.delenv("ECS_WORKER_TASK_DEFINITION", raising=False)
    assert worker_runtime.ecs_enabled() is False
    assert worker_runtime.runtime_name() == "docker"


def test_ecs_requires_both_vars(monkeypatch):
    monkeypatch.setenv("ECS_CLUSTER_NAME", "c")
    monkeypatch.delenv("ECS_WORKER_TASK_DEFINITION", raising=False)
    assert worker_runtime.ecs_enabled() is False


def test_ecs_enabled_with_both_vars(ecs_env):
    assert worker_runtime.ecs_enabled() is True
    assert worker_runtime.runtime_name() == "ecs"


# ---------------------------------------------------------------------------
# Docker start
# ---------------------------------------------------------------------------


async def test_start_worker_docker(monkeypatch):
    monkeypatch.delenv("ECS_CLUSTER_NAME", raising=False)
    fake_container = MagicMock(id="abcdef0123456789deadbeef")
    fake_client = MagicMock()
    fake_client.containers.get.side_effect = Exception("no self container")
    fake_client.containers.run.return_value = fake_container

    with patch("worker_runtime._get_docker_client", AsyncMock(return_value=fake_client)):
        spawned = await worker_runtime.start_worker_container(
            env={"PIONEER_GUILD_ID": "g1"}, guild_id="g1"
        )

    assert spawned.runtime == "docker"
    assert spawned.handle == "abcdef0123456789deadbeef"
    assert spawned.short_id == "abcdef012345"
    run_kwargs = fake_client.containers.run.call_args.kwargs
    assert run_kwargs["environment"] == {"PIONEER_GUILD_ID": "g1"}
    assert run_kwargs["labels"]["com.pioneer.kind"] == "worker"
    assert run_kwargs["detach"] is True


# ---------------------------------------------------------------------------
# ECS start
# ---------------------------------------------------------------------------


async def test_start_worker_ecs(ecs_env):
    fake_ecs = MagicMock()
    fake_ecs.run_task.return_value = {"tasks": [{"taskArn": TASK_ARN}], "failures": []}

    with patch("worker_runtime._get_ecs_client", return_value=fake_ecs):
        spawned = await worker_runtime.start_worker_container(
            env={"PIONEER_GUILD_ID": "g1"}, guild_id="g1"
        )

    assert spawned.runtime == "ecs"
    assert spawned.handle == TASK_ARN
    assert spawned.short_id == "0123456789ab"
    kwargs = fake_ecs.run_task.call_args.kwargs
    assert kwargs["cluster"] == "my-cluster"
    assert kwargs["taskDefinition"] == "my-worker-taskdef"
    assert kwargs["count"] == 1
    net = kwargs["networkConfiguration"]["awsvpcConfiguration"]
    assert net["subnets"] == ["subnet-1", "subnet-2"]
    assert net["securityGroups"] == ["sg-1"]
    assert net["assignPublicIp"] == "DISABLED"
    override = kwargs["overrides"]["containerOverrides"][0]
    assert override["name"] == "worker"
    assert {"name": "PIONEER_GUILD_ID", "value": "g1"} in override["environment"]


async def test_start_worker_ecs_public_ip_opt_in(ecs_env, monkeypatch):
    monkeypatch.setenv("ECS_WORKER_ASSIGN_PUBLIC_IP", "true")
    fake_ecs = MagicMock()
    fake_ecs.run_task.return_value = {"tasks": [{"taskArn": TASK_ARN}], "failures": []}

    with patch("worker_runtime._get_ecs_client", return_value=fake_ecs):
        await worker_runtime.start_worker_container(env={}, guild_id="g1")

    net = fake_ecs.run_task.call_args.kwargs["networkConfiguration"]["awsvpcConfiguration"]
    assert net["assignPublicIp"] == "ENABLED"


async def test_start_worker_ecs_requires_subnets(ecs_env, monkeypatch):
    monkeypatch.delenv("ECS_WORKER_SUBNETS", raising=False)
    fake_ecs = MagicMock()

    with (
        patch("worker_runtime._get_ecs_client", return_value=fake_ecs),
        pytest.raises(worker_runtime.WorkerSpawnError, match="ECS_WORKER_SUBNETS"),
    ):
        await worker_runtime.start_worker_container(env={}, guild_id="g1")

    fake_ecs.run_task.assert_not_called()


async def test_start_worker_ecs_surfaces_failures(ecs_env):
    fake_ecs = MagicMock()
    fake_ecs.run_task.return_value = {
        "tasks": [],
        "failures": [{"reason": "RESOURCE:MEMORY", "detail": "no capacity"}],
    }

    with (
        patch("worker_runtime._get_ecs_client", return_value=fake_ecs),
        pytest.raises(worker_runtime.WorkerSpawnError, match="RESOURCE:MEMORY"),
    ):
        await worker_runtime.start_worker_container(env={}, guild_id="g1")


# ---------------------------------------------------------------------------
# Stop dispatch
# ---------------------------------------------------------------------------


async def test_stop_dispatches_ecs_for_task_arns(monkeypatch):
    # No ECS env needed: the cluster is parsed out of the task ARN itself.
    monkeypatch.delenv("ECS_CLUSTER_NAME", raising=False)
    fake_ecs = MagicMock()

    with patch("worker_runtime._get_ecs_client", return_value=fake_ecs):
        await worker_runtime.stop_worker_container(TASK_ARN, reason="test kill")

    fake_ecs.stop_task.assert_called_once_with(
        cluster="my-cluster", task=TASK_ARN, reason="test kill"
    )


async def test_stop_dispatches_docker_for_container_ids():
    fake_container = MagicMock()
    fake_client = MagicMock()
    fake_client.containers.get.return_value = fake_container

    with patch("worker_runtime._get_docker_client", AsyncMock(return_value=fake_client)):
        await worker_runtime.stop_worker_container("abcdef012345")

    fake_client.containers.get.assert_called_once_with("abcdef012345")
    fake_container.kill.assert_called_once()
