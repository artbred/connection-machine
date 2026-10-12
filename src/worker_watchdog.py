"""Restart a stalled worker without issuing browser actions from another thread."""

import faulthandler
import os
import threading
import time


WORKER_STALL_TIMEOUT_SECONDS = 15 * 60


class WorkerWatchdog:
    """Bound startup, synchronous dispatcher polls, and browser shutdown.

    Playwright/Patchright reads such as count() and evaluate() can wait forever
    despite the page's default timeout. A separate thread must terminate the
    process: graceful browser cleanup can block on that same protocol channel.
    Docker restarts the worker; persisted processing invites remain quarantined
    by the dispatcher's existing interrupted-task recovery.
    """

    def __init__(self, timeout_seconds: float = WORKER_STALL_TIMEOUT_SECONDS):
        self._timeout = timeout_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._deadline = time.monotonic() + timeout_seconds
        self._stage = "startup"
        self._thread = threading.Thread(
            target=self._watch, name="worker-watchdog", daemon=True
        )

    def start(self):
        self.heartbeat("startup")
        self._thread.start()

    def heartbeat(self, stage: str):
        with self._lock:
            self._deadline = time.monotonic() + self._timeout
            self._stage = stage

    def stop(self):
        self._stop.set()
        self._thread.join()

    def _watch(self):
        while not self._stop.is_set():
            with self._lock:
                remaining = self._deadline - time.monotonic()
                stage = self._stage
            if remaining > 0:
                self._stop.wait(remaining)
                continue
            if self._stop.is_set():
                return
            try:
                # Do not acquire logging locks held by a stalled main thread.
                os.write(
                    2,
                    (
                        f"Worker stalled during {stage} for {self._timeout:g}s; "
                        "exiting for restart. Interrupted invites will not be replayed.\n"
                    ).encode(),
                )
                faulthandler.dump_traceback()
            finally:
                os._exit(1)
