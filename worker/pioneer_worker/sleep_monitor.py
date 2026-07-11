"""macOS sleep/wake awareness for the worker's WebSocket reconnect loop.

Wraps ``NSWorkspace`` sleep/wake notifications so the WS reconnect loop can
avoid hammering a dead network link while the laptop is asleep, and give the
network stack a couple of seconds to come back up after wake before the first
post-wake retry. No-op stub everywhere else (non-macOS, or ``pyobjc`` not
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
            self._monitor._handle_will_sleep()

        def didWake_(self, notification) -> None:
            self._monitor._handle_did_wake()


class SystemSleepMonitor:
    """Tracks macOS sleep/wake state on a background thread.

    ``is_sleeping`` is set on ``NSWorkspaceWillSleepNotification`` and stays
    set through a short grace period after ``NSWorkspaceDidWakeNotification``
    fires, so a caller checking ``is_sleeping.is_set()`` won't race a
    reconnect attempt against a network interface that hasn't come back up
    yet.

    ``on_sleep``/``on_wake`` callbacks run on the monitor's background
    thread — callers driving an asyncio loop must hop back onto it
    themselves (e.g. via ``call_soon_threadsafe``/``run_coroutine_threadsafe``).
    """

    def __init__(
        self,
        *,
        on_sleep: Callable[[], None] | None = None,
        on_wake: Callable[[], None] | None = None,
        wake_grace_seconds: float = 2.0,
    ) -> None:
        self.on_sleep = on_sleep
        self.on_wake = on_wake
        self._wake_grace_seconds = wake_grace_seconds
        # threading.Event rather than a bare bool: it's set on the monitor's
        # background thread and read from the asyncio event loop thread, so
        # the flag needs to be an explicit, thread-safe cross-thread signal.
        self.is_sleeping = threading.Event()
        self._thread: threading.Thread | None = None
        self._observer = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if not _HAS_APPKIT:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="sleep-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        if self._observer is not None:
            try:
                AppKit.NSWorkspace.sharedWorkspace().notificationCenter().removeObserver_(
                    self._observer
                )
            except Exception:
                logger.debug("removeObserver_ during stop() raised (ignored)", exc_info=True)
        self._thread.join(timeout=2.0)
        self._thread = None
        self._observer = None

    def _run(self) -> None:
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
        run_loop = AppKit.NSRunLoop.currentRunLoop()
        try:
            # Pump the run loop in short slices so the notification center can
            # deliver, while still checking _stop_event for clean shutdown.
            while not self._stop_event.is_set():
                run_loop.runUntilDate_(AppKit.NSDate.dateWithTimeIntervalSinceNow_(1.0))
        finally:
            center.removeObserver_(observer)

    def _handle_will_sleep(self) -> None:
        self.is_sleeping.set()
        logger.info("System sleep detected — pausing WS reconnects")
        if self.on_sleep is not None:
            try:
                self.on_sleep()
            except Exception:
                logger.exception("on_sleep callback raised")

    def _handle_did_wake(self) -> None:
        # Debounce on a throwaway thread so the run loop thread stays free to
        # keep pumping notifications during the grace period.
        threading.Thread(target=self._finish_wake, name="sleep-monitor-wake", daemon=True).start()

    def _finish_wake(self) -> None:
        time.sleep(self._wake_grace_seconds)
        self.is_sleeping.clear()
        logger.info("System wake detected — resuming WS reconnects")
        if self.on_wake is not None:
            try:
                self.on_wake()
            except Exception:
                logger.exception("on_wake callback raised")
