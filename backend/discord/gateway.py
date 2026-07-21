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
Intent" and "Server Members Intent" turned on in the Developer Portal (Bot
page) — without them Discord rejects the IDENTIFY payload's
``MESSAGE_CONTENT``/``GUILD_MEMBERS`` intent bits.

Protocol handled, per https://discord.com/developers/docs/topics/gateway:
    HELLO (op 10)            -> start the heartbeat loop, then IDENTIFY/RESUME
    HEARTBEAT_ACK (op 11)    -> mark the last heartbeat as acknowledged
    DISPATCH (op 0)          -> READY stores session_id/resume url; MESSAGE_CREATE
                                 is filtered and queued; GUILD_MEMBER_ADD sends a
                                 welcome DM (see ``discord_notifier.send_welcome_dm``)
    RECONNECT (op 7)         -> close and reconnect, attempting RESUME
    INVALID_SESSION (op 9)   -> RESUME if resumable, otherwise fresh IDENTIFY

Filtering applied to ``MESSAGE_CREATE`` before anything is queued:
    - ``author.bot is True`` and either this is our own application's bot user
      (``DISCORD_APPLICATION_ID``) or that env var isn't set at all -> discarded
      (avoids echoing discord_notifier; see ``_is_own_bot_or_unconfigured``).
      Other bots' messages pass through once ``DISCORD_APPLICATION_ID`` is
      configured, and get their own auto-provisioned ``users`` row on first
      contact (``discord/bot_users.py``) instead of being dropped outright.
    - foreman role @-mentioned    -> queued unconditionally (see below)
    - no ``guild_id`` (a DM)      -> discarded
    - channel/thread not "wired"  -> discarded (see ``_is_channel_wired``)

A channel or thread counts as "wired" if it is either a channel bound via
``/join-channel`` (``discord_channel_guilds``), or a thread Pioneer Square
itself created or knows how to route — any row in the unified
``discord_thread_bindings`` table (#885): a per-PR/issue thread, a per-task
live-stream thread, or a legacy, no-longer-created dated Foreman thread (kept
only so previously existing threads keep routing). Bound threads are child
threads of a wired channel but carry their own ``channel_id`` in Discord's
model, so they need their own lookup; the routing/reply layer (#744) does its
own, more specific, reverse-lookup against that same table to decide *which*
Foreman session a message belongs to.

One exception to the "wired" requirement: a message that @-mentions the
foreman role (``discord.auth.mentions_foreman_role``) is queued regardless of
guild/DM/wiring — this is a *user*-scoped path, not a channel-scoped one, so
the router (not this transport layer) is what decides which Foreman session
it belongs to (see ``discord/router.py``'s module docstring). The Gateway
must carry the ``DIRECT_MESSAGES`` intent (folded into ``_INTENTS`` below) for
a DM's ``MESSAGE_CREATE`` to reach this handler at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time

import websockets
from discord.auth import mentions_foreman_role
from discord_notifier import send_welcome_dm

logger = logging.getLogger(__name__)

_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

# GUILDS (1 << 0) | GUILD_MEMBERS (1 << 1) | GUILD_MESSAGES (1 << 9)
# | DIRECT_MESSAGES (1 << 12) | MESSAGE_CONTENT (1 << 15)
# DIRECT_MESSAGES is required so a DM's MESSAGE_CREATE reaches the bot at
# all — otherwise a foreman-role mention typed into a DM would never arrive.
_INTENTS = 33283 | 4096

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


def _is_own_bot_or_unconfigured(author_id: str | None) -> bool:
    """Return True if *author_id* is this application's own bot user, or if
    ``DISCORD_APPLICATION_ID`` isn't set (in which case we can't tell, so we
    conservatively treat every bot as "our own" to preserve the pre-existing,
    echo-safe default of dropping all bot messages).

    Set ``DISCORD_APPLICATION_ID`` to opt in to processing *other* bots'
    messages (auto-provisioned as their own ``users`` row — see
    ``discord/bot_users.py``) while still never reacting to our own messages.
    """
    own_id = os.environ.get("DISCORD_APPLICATION_ID")
    return not own_id or str(author_id) == own_id


def _backoff_delay(attempt: int, base: float = 2.0, cap: float = 60.0) -> float:
    """Exponential backoff with full jitter."""
    return min(cap, base * (2**attempt)) + random.uniform(0, base)


# A connection must stay up at least this long to count as "healthy" and reset
# the backoff. Without it, a socket that reaches READY then drops instantly
# (flapping) would reconnect with zero delay — the same storm the backoff
# prevents, just past the handshake.
_HEALTHY_MIN_SECONDS = 30.0

# Discord caps IDENTIFY (a fresh session, not a RESUME) at ~1000/day and closes
# abusive reconnect loops. Cap consecutive IDENTIFYs and cool down hard past it
# so an INVALID_SESSION loop can't quietly burn the daily identify budget.
_MAX_IDENTIFIES = 5
_IDENTIFY_COOLDOWN = 300.0  # fallback when READY didn't supply a reset window


# Short-lived cache so a busy wired channel doesn't open a DB session on
# every single message. Keyed on channel_id -> (wired, expires_at_monotonic).
_CHANNEL_WIRED_CACHE_TTL = 30.0
_channel_wired_cache: dict[str, tuple[bool, float]] = {}


async def _is_channel_wired(channel_id: str) -> bool:
    """Return True if *channel_id* (a channel or thread) has somewhere to route to.

    Backed by ``discord_channel_guilds`` (bound via ``/join-channel``) and the
    unified ``discord_thread_bindings`` table (every thread Pioneer Square
    created or reads, regardless of kind) — through a short TTL cache so
    repeated messages in the same channel/thread don't each pay for a DB
    round-trip.
    """
    now = time.monotonic()
    cached = _channel_wired_cache.get(channel_id)
    if cached is not None and cached[1] > now:
        return cached[0]

    wired = await _query_channel_wired(channel_id)
    _channel_wired_cache[channel_id] = (wired, now + _CHANNEL_WIRED_CACHE_TTL)
    return wired


async def _query_channel_wired(channel_id: str) -> bool:
    """Query for a routable binding, bypassing the cache.

    Checks the plain channel binding table first (cheapest/most common case),
    then the single ``discord_thread_bindings`` table — a message posted
    inside any thread Pioneer Square created (per-PR/issue, per-task stream,
    or legacy per-guild-daily) carries that thread's own ID as ``channel_id``,
    not its parent channel's. Never raises — DB errors are treated as "not
    wired".
    """
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import DiscordChannelGuild, DiscordThreadBinding  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(DiscordChannelGuild.id).where(
                    col(DiscordChannelGuild.discord_channel_id) == channel_id
                )
            )
            if result.first() is not None:
                return True

            result = await db.exec(
                select(DiscordThreadBinding.id).where(
                    col(DiscordThreadBinding.thread_id) == channel_id
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
        self._healthy = False
        # #2: throttle fresh IDENTIFYs. Count resets when a connection proves
        # healthy; reset_after is Discord's own identify-window from READY.
        self._identify_count = 0
        self._identify_reset_after = 0.0

    async def run(self) -> None:
        """Connect and process Gateway events forever. Never returns normally."""
        attempt = 0
        while True:
            url = self._resume_url if self._session_id else _GATEWAY_URL
            self._healthy = False
            started = time.monotonic()
            try:
                async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
                    await self._handle_connection(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # str(ConnectionClosed) carries Discord's close code + reason
                # (e.g. 4014 disallowed intents, 4004 auth failed) — the key
                # signal for why a session keeps ending. Include the type for
                # non-close errors (DNS, TLS, timeouts).
                logger.warning("discord gateway: connection error [%s] %s", type(exc).__name__, exc)
            finally:
                await self._stop_heartbeat()

            uptime = time.monotonic() - started

            # Reset the backoff only for a connection that reached READY/RESUMED
            # *and* stayed up a while. Resetting on mere socket-open (the
            # original bug) let a connect-then-immediately-closed failure — bad
            # token, or privileged intents disabled (close 4014) — retry every
            # ~3s forever, tripping Discord's abuse guard. The uptime floor also
            # covers a connection that reaches READY then drops instantly
            # (flapping): still a zero-delay storm without it.
            if self._healthy and uptime >= _HEALTHY_MIN_SECONDS:
                attempt = 0
                self._identify_count = 0
            else:
                delay = _backoff_delay(attempt)
                # "healthy but short" here == flapping: reached READY/RESUMED
                # then dropped almost immediately. Distinguish it from a
                # never-connected failure so the logs show which one is spinning.
                logger.warning(
                    "discord gateway: %s after %.1fs — reconnecting in %.1fs (attempt %d, identifies=%d)",
                    "flapping" if self._healthy else "connect failed",
                    uptime,
                    delay,
                    attempt + 1,
                    self._identify_count,
                )
                await asyncio.sleep(delay)
                attempt += 1

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
            logger.info(
                "discord gateway: RESUMEing session_id=%s seq=%s", self._session_id, self._seq
            )
            await self._send_resume(ws)
        else:
            logger.info(
                "discord gateway: IDENTIFYing (no resumable session; session_id=%s seq=%s)",
                self._session_id,
                self._seq,
            )
            await self._send_identify(ws)

        async for raw in ws:
            await self._handle_frame(ws, raw)

    async def _heartbeat_loop(self, ws, interval: float) -> None:
        # Discord: jitter the first heartbeat instead of firing exactly on the interval.
        await asyncio.sleep(interval * random.random())
        while True:
            if not self._heartbeat_acked:
                logger.warning("discord gateway: heartbeat not acked — forcing reconnect")
                # A local close() with a non-1000/1001 code makes `websockets`
                # raise ConnectionClosedError out of the concurrent
                # `async for raw in ws` read loop in _handle_connection
                # immediately (verified against websockets 16.0) — so this
                # unblocks the reader promptly rather than leaving it hung,
                # and run()'s except Exception handles the reconnect/backoff.
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
        # #2: a runaway INVALID_SESSION loop re-IDENTIFYs every 1–5s; cap it so
        # it can't burn Discord's ~1000/day identify budget. RESUME is exempt
        # (Discord doesn't rate-limit it), so only this path is counted.
        if self._identify_count >= _MAX_IDENTIFIES:
            wait = self._identify_reset_after or _IDENTIFY_COOLDOWN
            logger.warning(
                "discord gateway: %d consecutive IDENTIFYs — cooling down %.0fs "
                "to stay under Discord's identify limit",
                self._identify_count,
                wait,
            )
            await asyncio.sleep(wait)
            self._identify_count = 0
        self._identify_count += 1
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
            # Stop the heartbeat first so it can't send/close against a socket
            # we're already tearing down here (that raced "warning" was the
            # heartbeat loop hitting ConnectionClosed on its own).
            await self._stop_heartbeat()
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
            self._healthy = True
            # Remember Discord's identify-window so the #2 throttle cools down
            # for exactly as long as Discord says, not a guessed fallback.
            reset_after = (payload.get("session_start_limit") or {}).get("reset_after")
            if reset_after:
                self._identify_reset_after = reset_after / 1000.0
            self._session_id = payload.get("session_id")
            self._resume_url = payload.get("resume_gateway_url") or _GATEWAY_URL
            logger.info("discord gateway: READY session_id=%s", self._session_id)
        elif event_type == "RESUMED":
            self._healthy = True
            logger.info("discord gateway: RESUMED session_id=%s", self._session_id)
        elif event_type == "MESSAGE_CREATE":
            await self._handle_message_create(payload)
        elif event_type == "GUILD_MEMBER_ADD":
            await self._handle_guild_member_add(payload)

    async def _handle_message_create(self, message: dict) -> None:
        author = message.get("author") or {}
        if author.get("bot") and _is_own_bot_or_unconfigured(author.get("id")):
            return
        if mentions_foreman_role(message):
            # User-scoped, not channel-scoped — bypass the DM/wiring filters
            # below entirely; the router decides how to handle it from here.
            await self._queue.put(message)
            return
        if not message.get("guild_id"):
            return  # DM — no guild_id on the message
        channel_id = message.get("channel_id")
        if not channel_id or not await _is_channel_wired(channel_id):
            return
        await self._queue.put(message)

    async def _handle_guild_member_add(self, member: dict) -> None:
        """DM a newly-joined guild member onboarding instructions.

        Skips bot accounts (they can't run slash commands anyway) and members
        with no resolvable user id. Delegates the actual DM — including
        swallowing "DMs disabled" failures — to
        ``discord_notifier.send_welcome_dm``, so this method never raises.
        """
        user = member.get("user") or {}
        user_id = user.get("id")
        if not user_id or user.get("bot"):
            return
        username = user.get("global_name") or user.get("username")

        await send_welcome_dm(user_id, username)


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
