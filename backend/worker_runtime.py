"""Container runtime abstraction for spawning and killing worker processes.

Two runtimes are supported, selected automatically from the environment:

- **docker** — the docker-compose / local-dev model: the backend talks to the
  mounted Docker socket (``docker.from_env()``) and runs one worker container
  per spawn on its own network.
- **ecs** — the AWS Fargate model: the backend calls ``ecs.run_task`` against
  the pre-registered worker task definition family. Fargate tasks have no
  shared Docker socket, so this is the only way to launch workers there. The
  Terraform module in ``terraform/`` registers the task definition, grants the
  backend's task role ``ecs:RunTask``/``ecs:StopTask``/``ecs:DescribeTasks``,
  and injects the env vars below into the backend service.

ECS mode is enabled when both ``ECS_CLUSTER_NAME`` and
``ECS_WORKER_TASK_DEFINITION`` are set (as they are in the ECS task definition
rendered by terraform/ecs.tf). Additional ECS configuration:

- ``ECS_WORKER_SUBNETS`` — comma-separated subnet IDs for the worker task's
  awsvpc network interface (required to run on Fargate).
- ``ECS_WORKER_SECURITY_GROUPS`` — comma-separated security group IDs.
- ``ECS_WORKER_ASSIGN_PUBLIC_IP`` — "true" to give worker tasks a public IP
  (default false; private subnets with a NAT gateway don't need one).
- ``ECS_WORKER_CONTAINER_NAME`` — container name inside the worker task
  definition to apply env overrides to (default "worker").

The spawn *handle* persisted in ``workers.container_id`` is the Docker
container ID in docker mode and the full ECS task ARN in ECS mode;
``stop_worker_container`` dispatches on the ``arn:`` prefix so either kind of
handle can be force-killed after a redeploy regardless of the current mode.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Hard cap on a single spawn call. The Docker/ECS API call should return as
# soon as the runtime accepts the task, but can legitimately take a while on a
# loaded host (image pull, capacity placement).
CONTAINER_RUN_TIMEOUT = 600

# Hard cap on a single force-kill call.
CONTAINER_STOP_TIMEOUT = 60

# Module-level clients — created once per process to reuse connections across
# repeated spawn calls. Tests patch _get_docker_client / _get_ecs_client.
_docker_client: Any = None
_ecs_client: Any = None


class WorkerSpawnError(Exception):
    """Raised when the runtime accepted our request but could not start a worker.

    ``status_code`` is a hint for HTTP callers (404 for a missing image,
    503 for an unavailable runtime, 500 otherwise).
    """

    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class SpawnedWorker:
    """Result of a successful spawn."""

    handle: str  # Docker container ID or ECS task ARN — persist in workers.container_id
    short_id: str  # 12-char display form for logs / tool results
    runtime: str  # "docker" | "ecs"


def ecs_enabled() -> bool:
    """True when the backend should dispatch workers via ECS RunTask."""
    return bool(os.environ.get("ECS_CLUSTER_NAME") and os.environ.get("ECS_WORKER_TASK_DEFINITION"))


def runtime_name() -> str:
    return "ecs" if ecs_enabled() else "docker"


async def check_runtime_available() -> None:
    """Raise WorkerSpawnError(503) if the selected runtime cannot spawn workers."""
    if ecs_enabled():
        try:
            _get_ecs_client()
        except ImportError:
            raise WorkerSpawnError("boto3 not installed in backend", status_code=503)
        except Exception as e:
            raise WorkerSpawnError(f"ECS client unavailable: {e}", status_code=503)
        return
    try:
        await _get_docker_client()
    except ImportError:
        raise WorkerSpawnError("Docker SDK not installed in backend", status_code=503)
    except Exception as e:
        raise WorkerSpawnError(f"Docker socket unavailable: {e}", status_code=503)


async def _get_docker_client() -> Any:
    """Return the cached Docker client, importing the SDK and calling from_env() once."""
    global _docker_client
    if _docker_client is not None:
        return _docker_client
    import docker  # noqa: PLC0415 — ImportError propagated to caller if SDK not installed

    _docker_client = await asyncio.to_thread(docker.from_env)
    return _docker_client


def _get_ecs_client() -> Any:
    """Return the cached boto3 ECS client (region from the usual AWS env/config chain)."""
    global _ecs_client
    if _ecs_client is not None:
        return _ecs_client
    import boto3  # noqa: PLC0415 — ImportError propagated to caller if SDK not installed

    _ecs_client = boto3.client("ecs")
    return _ecs_client


def _csv_env(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


async def start_worker_container(*, env: dict[str, str], guild_id: str) -> SpawnedWorker:
    """Start one worker with the given environment; returns its runtime handle.

    Raises WorkerSpawnError on failure (including a friendly message for a
    missing local image) and ImportError if the runtime SDK isn't installed.
    """
    if ecs_enabled():
        return await _start_worker_ecs(env=env, guild_id=guild_id)
    return await _start_worker_docker(env=env, guild_id=guild_id)


async def stop_worker_container(handle: str, *, reason: str = "killed by backend") -> None:
    """Force-kill the container/task behind *handle* (best effort, raises on API errors).

    Dispatches on the handle itself rather than the current runtime so ECS task
    ARNs recorded before a config change are still stoppable, and vice versa.
    """
    if handle.startswith("arn:"):
        await _stop_worker_ecs(handle, reason=reason)
    else:
        await _stop_worker_docker(handle)


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------


async def _start_worker_docker(*, env: dict[str, str], guild_id: str) -> SpawnedWorker:
    client = await _get_docker_client()
    image = os.environ.get("WORKER_IMAGE", "pioneer-square-worker")

    # Join the same Docker network as the backend so the worker can reach it.
    network = None
    try:
        me = await asyncio.to_thread(client.containers.get, os.environ.get("HOSTNAME", ""))
        network = next(iter(me.attrs["NetworkSettings"]["Networks"].keys()), None)
    except Exception as e:
        logger.warning("Docker network detection failed, starting without explicit network: %s", e)

    # Pioneer-owned labels — these containers are spawned and lifecycle-managed
    # by the backend, not compose. List with:
    # docker ps --filter label=com.pioneer.kind=worker
    run_kwargs: dict = dict(
        image=image,
        environment=env,
        detach=True,
        remove=True,
        labels={"com.pioneer.kind": "worker", "com.pioneer.guild": guild_id},
        name=f"pioneer-worker-{guild_id}-{secrets.token_hex(3)}",
    )
    if network:
        run_kwargs["network"] = network

    try:
        container = await asyncio.wait_for(
            asyncio.to_thread(client.containers.run, **run_kwargs),
            timeout=CONTAINER_RUN_TIMEOUT,
        )
    except Exception as exc:
        # Import lazily inside the handler: the docker SDK is only guaranteed
        # importable when the (possibly test-patched) client above is real.
        try:
            import docker as docker_sdk  # noqa: PLC0415

            image_not_found = isinstance(exc, docker_sdk.errors.ImageNotFound)
        except ImportError:
            image_not_found = False
        if image_not_found:
            raise WorkerSpawnError(
                f"Worker image '{image}' not found — run: docker compose build worker",
                status_code=404,
            )
        raise
    return SpawnedWorker(handle=container.id, short_id=container.id[:12], runtime="docker")


async def _stop_worker_docker(container_id: str) -> None:
    client = await _get_docker_client()
    container = await asyncio.wait_for(
        asyncio.to_thread(client.containers.get, container_id),
        timeout=CONTAINER_STOP_TIMEOUT,
    )
    await asyncio.wait_for(asyncio.to_thread(container.kill), timeout=CONTAINER_STOP_TIMEOUT)


# ---------------------------------------------------------------------------
# ECS
# ---------------------------------------------------------------------------


async def _start_worker_ecs(*, env: dict[str, str], guild_id: str) -> SpawnedWorker:
    client = _get_ecs_client()
    cluster = os.environ["ECS_CLUSTER_NAME"]
    task_definition = os.environ["ECS_WORKER_TASK_DEFINITION"]
    container_name = os.environ.get("ECS_WORKER_CONTAINER_NAME", "worker")

    run_kwargs: dict = dict(
        cluster=cluster,
        taskDefinition=task_definition,
        launchType="FARGATE",
        count=1,
        # startedBy is capped at 36 chars; guild slugs are 6 chars.
        startedBy=f"pioneer-backend/{guild_id}"[:36],
        group=f"pioneer-worker:{guild_id}",
        overrides={
            "containerOverrides": [
                {
                    "name": container_name,
                    # NOTE: the RunTask overrides payload is limited to 8 KiB.
                    # Static secrets (GITHUB_TOKEN, CLAUDE_CODE_OAUTH_TOKEN, …)
                    # already ride in the task definition's `secrets` block, so
                    # this override only needs the per-spawn PIONEER_* vars,
                    # but a huge extra_env from the UI could still exceed it.
                    "environment": [{"name": k, "value": v} for k, v in env.items()],
                }
            ]
        },
    )
    subnets = _csv_env("ECS_WORKER_SUBNETS")
    if subnets:
        assign_public_ip = os.environ.get("ECS_WORKER_ASSIGN_PUBLIC_IP", "").lower() in (
            "1",
            "true",
            "yes",
        )
        vpc_config: dict = {
            "subnets": subnets,
            "assignPublicIp": "ENABLED" if assign_public_ip else "DISABLED",
        }
        security_groups = _csv_env("ECS_WORKER_SECURITY_GROUPS")
        if security_groups:
            vpc_config["securityGroups"] = security_groups
        run_kwargs["networkConfiguration"] = {"awsvpcConfiguration": vpc_config}
    else:
        # awsvpc-mode task definitions (all Fargate ones) can't launch without
        # a network configuration — fail with a pointer at the missing config
        # rather than an opaque boto3 ClientError.
        raise WorkerSpawnError(
            "ECS worker dispatch is enabled but ECS_WORKER_SUBNETS is not set — "
            "the Fargate worker task needs at least one subnet ID"
        )

    response = await asyncio.wait_for(
        asyncio.to_thread(client.run_task, **run_kwargs),
        timeout=CONTAINER_RUN_TIMEOUT,
    )

    failures = response.get("failures") or []
    tasks = response.get("tasks") or []
    if not tasks:
        detail = "; ".join(
            f"{f.get('reason', 'unknown')}" + (f" ({f['detail']})" if f.get("detail") else "")
            for f in failures
        )
        raise WorkerSpawnError(f"ECS RunTask returned no task: {detail or 'no failure detail'}")

    task_arn: str = tasks[0]["taskArn"]
    # The last ARN path segment is the 32-hex task ID — use its head as the display id.
    return SpawnedWorker(handle=task_arn, short_id=task_arn.rsplit("/", 1)[-1][:12], runtime="ecs")


async def _stop_worker_ecs(task_arn: str, *, reason: str) -> None:
    client = _get_ecs_client()
    # StopTask needs the cluster the task runs in; the cluster name is the
    # second-to-last ARN segment (task ARN format:
    # arn:aws:ecs:<region>:<acct>:task/<cluster>/<task-id>).
    parts = task_arn.rsplit("/", 2)
    cluster = parts[1] if len(parts) == 3 else os.environ.get("ECS_CLUSTER_NAME", "")
    await asyncio.wait_for(
        # ECS StopTask sends SIGTERM then SIGKILL after the task's stopTimeout.
        asyncio.to_thread(client.stop_task, cluster=cluster, task=task_arn, reason=reason[:255]),
        timeout=CONTAINER_STOP_TIMEOUT,
    )
