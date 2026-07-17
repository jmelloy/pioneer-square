"""macOS sleep/wake awareness for the worker's WebSocket reconnect loop.

Wraps ``NSWorkspace`` sleep/wake notifications so the WS reconnect loop can
avoid hammering a dead network link while the laptop is asleep, and only
resume after the system stays awake for a full grace period — brief Power
Nap / notification wakes are debounced away so the worker doesn't flap
online/offline on the backend. No-op stub everywhere else (non-macOS, or ``pyobjc`` not
installed): ``start()``/``stop()`` do nothing and ``is_sleeping`` stays unset.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

try:
    import AppKit
    import objc

    _HAS_APPKIT = True
except ImportError:
    _HAS_APPKIT = False

if _HAS_APPKIT:

    class _SleepWakeObserver(AppKit.NSObject):
        """Thin NSObject target that forwards NSWorkspace notifications."""

        def initWithMonitor_(self, monitor: SystemSleepMonitor):
            self = objc.super(_SleepWakeObserver, self).init()
            if self is None:
                return None
            self._monitor = monitor
            return self

        def willSleep_(self, notification) -> None:
            logger.debug("NSWorkspaceWillSleepNotification received: %r", notification)
            self._monitor._handle_will_sleep()

        def didWake_(self, notification) -> None:
            logger.debug("NSWorkspaceDidWakeNotification received: %r", notification)
            self._monitor._handle_did_wake()


class SystemSleepMonitor:
    """Tracks macOS sleep/wake state on a background thread.

    ``is_sleeping`` is set on ``NSWorkspaceWillSleepNotification`` and only
    clears after the system stays awake for ``wake_grace_seconds`` of
    continuous wall-clock time. The grace period is both a network-settle
    delay and a debounce: Power Nap / notification wakes light the machine
    up for a few seconds every couple of minutes, and reconnecting on each
    one makes the worker flap online/offline on the backend. A wake that
    doesn't outlast the grace period never resumes reconnects.

    ``on_sleep``/``on_wake`` callbacks run on the monitor's background
    thread — callers driving an asyncio loop must hop back onto it
    themselves (e.g. via ``call_soon_threadsafe``/``run_coroutine_threadsafe``).
    """

    def __init__(
        self,
        *,
        on_sleep: Callable[[], None] | None = None,
        on_wake: Callable[[], None] | None = None,
        wake_grace_seconds: float = 30.0,
    ) -> None:
        self.on_sleep = on_sleep
        self.on_wake = on_wake
        self._wake_grace_seconds = wake_grace_seconds
        # threading.Event rather than a bare bool: it's set on the monitor's
        # background thread and read from the asyncio event loop thread, so
        # the flag needs to be an explicit, thread-safe cross-thread signal.
        self.is_sleeping = threading.Event()
        # Bumped on every willSleep; a pending _finish_wake compares its
        # captured value and aborts if the system re-slept during the grace
        # period. Int read/write is atomic under the GIL — no lock needed.
        self._sleep_generation = 0
        self._thread: threading.Thread | None = None
        self._observer = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if not _HAS_APPKIT:
            logger.info("Sleep monitor start() no-op: AppKit/pyobjc unavailable (non-macOS?)")
            return
        if self._thread is not None:
            logger.warning("Sleep monitor start() called but thread already running; ignoring")
            return
        logger.info("Starting sleep monitor (wake_grace_seconds=%s)", self._wake_grace_seconds)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="sleep-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            logger.debug("Sleep monitor stop() no-op: not running")
            return
        logger.info("Stopping sleep monitor")
        self._stop_event.set()
        if self._observer is not None:
            try:
                AppKit.NSWorkspace.sharedWorkspace().notificationCenter().removeObserver_(
                    self._observer
                )
            except Exception:
                logger.debug("removeObserver_ during stop() raised (ignored)", exc_info=True)
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            logger.warning("Sleep monitor thread did not exit within 2s join timeout")
        else:
            logger.info("Sleep monitor stopped cleanly")
        self._thread = None
        self._observer = None

    def _run(self) -> None:
        logger.debug("Sleep monitor run loop thread started")
        workspace = AppKit.NSWorkspace.sharedWorkspace()
        center = workspace.notificationCenter()
        observer = _SleepWakeObserver.alloc().initWithMonitor_(self)
        center.addObserver_selector_name_object_(
            observer, "willSleep:", AppKit.NSWorkspaceWillSleepNotification, None
        )
        center.addObserver_selector_name_object_(
            observer, "didWake:", AppKit.NSWorkspaceDidWakeNotification, None
        )
        self._observer = observer
        logger.info("Registered NSWorkspace sleep/wake observers; entering run loop")
        run_loop = AppKit.NSRunLoop.currentRunLoop()
        try:
            # Pump the run loop in short slices so the notification center can
            # deliver, while still checking _stop_event for clean shutdown.
            while not self._stop_event.is_set():
                run_loop.runUntilDate_(AppKit.NSDate.dateWithTimeIntervalSinceNow_(1.0))
        except Exception:
            logger.exception("Sleep monitor run loop crashed")
            raise
        finally:
            logger.debug("Sleep monitor run loop exiting; removing observers")
            center.removeObserver_(observer)

    def _handle_will_sleep(self) -> None:
        already = self.is_sleeping.is_set()
        self.is_sleeping.set()
        self._sleep_generation += 1
        logger.info(
            "System sleep detected — pausing WS reconnects (was_already_sleeping=%s, "
            "on_sleep_callback=%s)",
            already,
            self.on_sleep is not None,
        )
        if self.on_sleep is not None:
            try:
                self.on_sleep()
                logger.debug("on_sleep callback completed")
            except Exception:
                logger.exception("on_sleep callback raised")

    def _handle_did_wake(self) -> None:
        # Debounce on a throwaway thread so the run loop thread stays free to
        # keep pumping notifications during the grace period.
        logger.info(
            "System wake detected — starting %ss grace period before resuming WS reconnects",
            self._wake_grace_seconds,
        )
        generation = self._sleep_generation
        threading.Thread(
            target=self._finish_wake, args=(generation,), name="sleep-monitor-wake", daemon=True
        ).start()

    def _finish_wake(self, generation: int) -> None:
        logger.debug("Wake grace period started (%ss)", self._wake_grace_seconds)
        time.sleep(self._wake_grace_seconds)
        if generation != self._sleep_generation:
            logger.info(
                "Wake grace period aborted — system re-entered sleep before %ss elapsed "
                "(likely a Power Nap / notification wake); reconnects stay paused",
                self._wake_grace_seconds,
            )
            return
        self.is_sleeping.clear()
        logger.info(
            "Wake grace period elapsed — resuming WS reconnects (on_wake_callback=%s)",
            self.on_wake is not None,
        )
        if self.on_wake is not None:
            try:
                self.on_wake()
                logger.debug("on_wake callback completed")
            except Exception:
                logger.exception("on_wake callback raised")
