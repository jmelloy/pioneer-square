"""Discord Gateway websocket client (Phase 4): transport for inbound messages.

This module owns a single persistent connection to Discord's Gateway
(``wss://gateway.discord.gg``) so the backend can receive ``MESSAGE_CREATE``
events in real time, instead of only the one-shot ``POST /discord/interactions``
webhook. It is transport-layer only: filtered events are handed to
``gateway_message_queue`` for a downstream consumer (#744, Foreman routing) to
read. This module never imports or calls into Foreman logic.

Enable with::

    DISCORD_GATEWAY_ENABLED=true
    DISCORD_BOT_TOKEN=...          # reused from Phase 2/3

The bot's Discord application must have the privileged "Message Content
Intent" turned on in the Developer Portal (Bot page) — without it Discord
rejects the IDENTIFY payload's ``MESSAGE_CONTENT`` intent bit.

Protocol handled, per https://discord.com/developers/docs/topics/gateway:
    HELLO (op 10)            -> start the heartbeat loop, then IDENTIFY/RESUME
    HEARTBEAT_ACK (op 11)    -> mark the last heartbeat as acknowledged
    DISPATCH (op 0)          -> READY stores session_id/resume url; MESSAGE_CREATE
                                 is filtered and queued
    RECONNECT (op 7)         -> close and reconnect, attempting RESUME
    INVALID_SESSION (op 9)   -> RESUME if resumable, otherwise fresh IDENTIFY

Filtering applied to ``MESSAGE_CREATE`` before anything is queued:
    - ``author.bot is True``      -> discarded (avoids echoing discord_notifier)
    - no ``guild_id`` (a DM)      -> discarded
    - channel not in ``discord_channel_guilds`` -> discarded (not wired)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random

import websockets

logger = logging.getLogger(__name__)

_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

# GUILDS (1 << 0) | GUILD_MESSAGES (1 << 9) | MESSAGE_CONTENT (1 << 15)
_INTENTS = 33281

# Gateway opcodes.
_OP_DISPATCH = 0
_OP_HEARTBEAT = 1
_OP_IDENTIFY = 2
_OP_RESUME = 6
_OP_RECONNECT = 7
_OP_INVALID_SESSION = 9
_OP_HELLO = 10
_OP_HEARTBEAT_ACK = 11

# Filtered MESSAGE_CREATE events for wired channels, consumed by #744.
gateway_message_queue: asyncio.Queue[dict] = asyncio.Queue()


def _bot_token() -> str | None:
    return os.environ.get("DISCORD_BOT_TOKEN") or None


def _gateway_enabled() -> bool:
    return os.environ.get("DISCORD_GATEWAY_ENABLED", "false").strip().lower() == "true"


def _backoff_delay(attempt: int, base: float = 2.0, cap: float = 60.0) -> float:
    """Exponential backoff with full jitter."""
    return min(cap, base * (2**attempt)) + random.uniform(0, base)


async def _is_channel_wired(channel_id: str) -> bool:
    """Return True if *channel_id* (a channel or thread) has a guild binding.

    Backed by the ``discord_channel_guilds`` table populated by
    ``/join-channel``. Never raises — DB errors are treated as "not wired".
    """
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import DiscordChannelGuild  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(DiscordChannelGuild.id).where(
                    col(DiscordChannelGuild.discord_channel_id) == channel_id
                )
            )
            return result.first() is not None
    except Exception:
        logger.warning(
            "discord gateway: channel binding lookup failed channel=%s", channel_id, exc_info=True
        )
        return False


class GatewayClient:
    """Persistent Discord Gateway connection.

    One instance owns exactly one logical session (across reconnects/resumes).
    Run it as a single long-lived task via ``run()``; it loops forever,
    reconnecting with backoff on any transport error.
    """

    def __init__(self, token: str, queue: asyncio.Queue | None = None) -> None:
        self._token = token
        self._queue = queue if queue is not None else gateway_message_queue
        self._session_id: str | None = None
        self._seq: int | None = None
        self._resume_url: str = _GATEWAY_URL
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_acked = True

    async def run(self) -> None:
        """Connect and process Gateway events forever. Never returns normally."""
        attempt = 0
        while True:
            url = self._resume_url if self._session_id else _GATEWAY_URL
            try:
                async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
                    attempt = 0
                    await self._handle_connection(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = _backoff_delay(attempt)
                logger.warning(
                    "discord gateway: connection error (%s) — retrying in %.1fs", exc, delay
                )
                await asyncio.sleep(delay)
                attempt += 1
            finally:
                await self._stop_heartbeat()

    async def _handle_connection(self, ws) -> None:
        """Run the HELLO handshake, then process frames until the socket closes."""
        hello = json.loads(await ws.recv())
        if hello.get("op") != _OP_HELLO:
            logger.warning("discord gateway: expected HELLO, got op=%s", hello.get("op"))
            return

        interval = hello["d"]["heartbeat_interval"] / 1000.0
        self._heartbeat_acked = True
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(ws, interval), name="discord-gateway-heartbeat"
        )

        if self._session_id and self._seq is not None:
            await self._send_resume(ws)
        else:
            await self._send_identify(ws)

        async for raw in ws:
            await self._handle_frame(ws, raw)

    async def _heartbeat_loop(self, ws, interval: float) -> None:
        # Discord: jitter the first heartbeat instead of firing exactly on the interval.
        await asyncio.sleep(interval * random.random())
        while True:
            if not self._heartbeat_acked:
                logger.warning("discord gateway: heartbeat not acked — forcing reconnect")
                await ws.close(code=4000)
                return
            self._heartbeat_acked = False
            await ws.send(json.dumps({"op": _OP_HEARTBEAT, "d": self._seq}))
            await asyncio.sleep(interval)

    async def _stop_heartbeat(self) -> None:
        if self._heartbeat_task is None:
            return
        self._heartbeat_task.cancel()
        try:
            await self._heartbeat_task
        except (asyncio.CancelledError, Exception):
            pass
        self._heartbeat_task = None

    async def _send_identify(self, ws) -> None:
        await ws.send(
            json.dumps(
                {
                    "op": _OP_IDENTIFY,
                    "d": {
                        "token": self._token,
                        "intents": _INTENTS,
                        "properties": {
                            "os": "linux",
                            "browser": "pioneer-square",
                            "device": "pioneer-square",
                        },
                    },
                }
            )
        )

    async def _send_resume(self, ws) -> None:
        await ws.send(
            json.dumps(
                {
                    "op": _OP_RESUME,
                    "d": {
                        "token": self._token,
                        "session_id": self._session_id,
                        "seq": self._seq,
                    },
                }
            )
        )

    async def _handle_frame(self, ws, raw) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("discord gateway: non-JSON frame ignored: %r", raw[:120])
            return

        if data.get("s") is not None:
            self._seq = data["s"]

        op = data.get("op")
        if op == _OP_DISPATCH:
            await self._handle_dispatch(data)
        elif op == _OP_HEARTBEAT_ACK:
            self._heartbeat_acked = True
        elif op == _OP_RECONNECT:
            logger.info("discord gateway: server requested reconnect")
            await ws.close()
        elif op == _OP_INVALID_SESSION:
            resumable = data.get("d") is True
            logger.warning("discord gateway: invalid session (resumable=%s)", resumable)
            if not resumable:
                self._session_id = None
                self._seq = None
                self._resume_url = _GATEWAY_URL
            await asyncio.sleep(1 + random.random() * 4)
            if resumable and self._session_id:
                await self._send_resume(ws)
            else:
                await self._send_identify(ws)

    async def _handle_dispatch(self, data: dict) -> None:
        event_type = data.get("t")
        payload = data.get("d") or {}
        if event_type == "READY":
            self._session_id = payload.get("session_id")
            self._resume_url = payload.get("resume_gateway_url") or _GATEWAY_URL
            logger.info("discord gateway: READY session_id=%s", self._session_id)
        elif event_type == "RESUMED":
            logger.info("discord gateway: RESUMED session_id=%s", self._session_id)
        elif event_type == "MESSAGE_CREATE":
            await self._handle_message_create(payload)

    async def _handle_message_create(self, message: dict) -> None:
        author = message.get("author") or {}
        if author.get("bot"):
            return
        if not message.get("guild_id"):
            return  # DM — no guild_id on the message
        channel_id = message.get("channel_id")
        if not channel_id or not await _is_channel_wired(channel_id):
            return
        await self._queue.put(message)


_gateway_task: asyncio.Task | None = None


def start_gateway() -> asyncio.Task | None:
    """Start the persistent Gateway task, spawned via ``util.tasks.spawn``.

    No-op (returns None) when ``DISCORD_GATEWAY_ENABLED`` is not ``true`` or
    ``DISCORD_BOT_TOKEN`` is unset. Safe to call more than once — returns the
    existing task if one is already running.
    """
    global _gateway_task
    if not _gateway_enabled():
        return None
    token = _bot_token()
    if not token:
        logger.warning(
            "discord gateway: DISCORD_GATEWAY_ENABLED=true but DISCORD_BOT_TOKEN is unset; not starting"
        )
        return None
    if _gateway_task is not None and not _gateway_task.done():
        return _gateway_task

    from util.tasks import spawn  # noqa: PLC0415 — avoid importing util at module load for tests

    _gateway_task = spawn(GatewayClient(token).run(), name="discord-gateway")
    return _gateway_task


async def stop_gateway() -> None:
    """Cancel the running Gateway task, if any, and wait for it to unwind."""
    global _gateway_task
    if _gateway_task is None:
        return
    _gateway_task.cancel()
    try:
        await _gateway_task
    except (asyncio.CancelledError, Exception):
        pass
    _gateway_task = None
