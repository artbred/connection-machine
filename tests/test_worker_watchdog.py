"""Verify process-level recovery, not just a timer callback or mock exit."""

import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkerWatchdogTests(unittest.TestCase):
    def run_worker(self, code):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_stalled_poll_exits_nonzero_without_running_cleanup(self):
        result = self.run_worker(
            """
import time
from worker_watchdog import WorkerWatchdog
watchdog = WorkerWatchdog(timeout_seconds=0.2)
watchdog.start()
watchdog.heartbeat('dispatcher poll')
try:
    time.sleep(5)
finally:
    print('unsafe cleanup reached', flush=True)
"""
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Worker stalled during dispatcher poll", result.stderr)
        self.assertNotIn("unsafe cleanup reached", result.stdout)

    def test_ongoing_progress_renews_deadline_and_shutdown_disarms_watchdog(self):
        result = self.run_worker(
            """
import time
from worker_watchdog import WorkerWatchdog
watchdog = WorkerWatchdog(timeout_seconds=0.5)
watchdog.start()
for _ in range(12):
    watchdog.heartbeat('dispatcher poll')
    time.sleep(0.1)
watchdog.stop()
time.sleep(0.7)
print('progress and shutdown completed', flush=True)
"""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "progress and shutdown completed")
        self.assertNotIn("Worker stalled", result.stderr)

    def test_startup_is_bounded_before_first_poll(self):
        result = self.run_worker(
            """
import time
from worker_watchdog import WorkerWatchdog
watchdog = WorkerWatchdog(timeout_seconds=0.2)
watchdog.start()
time.sleep(5)
"""
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Worker stalled during startup", result.stderr)


if __name__ == "__main__":
    unittest.main()
