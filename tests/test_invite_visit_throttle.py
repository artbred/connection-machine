import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from db import TaskType  # noqa: E402
from dispatcher import (  # noqa: E402
    TaskDispatcher,
    get_invite_visit_throttle_interval,
    DEFAULT_INVITE_VISIT_THROTTLE_SECONDS,
    INVITE_VISIT_THROTTLE_ENV,
)


class GetIntervalTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(INVITE_VISIT_THROTTLE_ENV)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(INVITE_VISIT_THROTTLE_ENV, None)
        else:
            os.environ[INVITE_VISIT_THROTTLE_ENV] = self._saved

    def test_default_when_unset(self):
        os.environ.pop(INVITE_VISIT_THROTTLE_ENV, None)
        interval = get_invite_visit_throttle_interval()
        self.assertIsNotNone(interval)
        low = DEFAULT_INVITE_VISIT_THROTTLE_SECONDS * 0.7
        high = DEFAULT_INVITE_VISIT_THROTTLE_SECONDS * 1.3
        self.assertGreaterEqual(interval.total_seconds(), low)
        self.assertLessEqual(interval.total_seconds(), high)

    def test_zero_disables(self):
        os.environ[INVITE_VISIT_THROTTLE_ENV] = "0"
        self.assertIsNone(get_invite_visit_throttle_interval())

    def test_negative_disables(self):
        os.environ[INVITE_VISIT_THROTTLE_ENV] = "-5"
        self.assertIsNone(get_invite_visit_throttle_interval())

    def test_custom_value_is_randomized_around_base(self):
        os.environ[INVITE_VISIT_THROTTLE_ENV] = "200"
        interval = get_invite_visit_throttle_interval()
        self.assertIsNotNone(interval)
        self.assertGreaterEqual(interval.total_seconds(), 200 * 0.7)
        self.assertLessEqual(interval.total_seconds(), 200 * 1.3)

    def test_invalid_falls_back_to_default(self):
        os.environ[INVITE_VISIT_THROTTLE_ENV] = "not-a-number"
        interval = get_invite_visit_throttle_interval()
        self.assertIsNotNone(interval)
        self.assertLessEqual(
            interval.total_seconds(), DEFAULT_INVITE_VISIT_THROTTLE_SECONDS * 1.3
        )


def _make_dispatcher():
    d = TaskDispatcher.__new__(TaskDispatcher)
    d.next_execution_at = {}
    d._invite_visit_throttle_until = None
    return d


class ApplyThrottleTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(INVITE_VISIT_THROTTLE_ENV)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(INVITE_VISIT_THROTTLE_ENV, None)
        else:
            os.environ[INVITE_VISIT_THROTTLE_ENV] = self._saved

    def test_apply_sets_future_throttle(self):
        os.environ[INVITE_VISIT_THROTTLE_ENV] = "90"
        d = _make_dispatcher()
        before = datetime.utcnow()
        d._apply_invite_visit_throttle()
        self.assertIsNotNone(d._invite_visit_throttle_until)
        self.assertGreater(d._invite_visit_throttle_until, before)

    def test_disabled_leaves_throttle_none(self):
        os.environ[INVITE_VISIT_THROTTLE_ENV] = "0"
        d = _make_dispatcher()
        d._apply_invite_visit_throttle()
        self.assertIsNone(d._invite_visit_throttle_until)

    def test_does_not_shorten_existing_long_spacing(self):
        os.environ[INVITE_VISIT_THROTTLE_ENV] = "90"
        d = _make_dispatcher()
        far = datetime.utcnow() + timedelta(hours=2)
        d.next_execution_at[TaskType.SEND_INVITE] = far
        d._apply_invite_visit_throttle()
        # A 2h real cooldown dominates the 90s throttle: no throttle recorded.
        self.assertIsNone(d._invite_visit_throttle_until)

    def test_extends_when_spacing_is_shorter_than_throttle(self):
        os.environ[INVITE_VISIT_THROTTLE_ENV] = "90"
        d = _make_dispatcher()
        d.next_execution_at[TaskType.SEND_INVITE] = datetime.utcnow() + timedelta(
            seconds=1
        )
        d._apply_invite_visit_throttle()
        self.assertIsNotNone(d._invite_visit_throttle_until)

    def test_does_not_shrink_existing_throttle(self):
        os.environ[INVITE_VISIT_THROTTLE_ENV] = "90"
        d = _make_dispatcher()
        far_throttle = datetime.utcnow() + timedelta(minutes=30)
        d._invite_visit_throttle_until = far_throttle
        d._apply_invite_visit_throttle()
        self.assertEqual(d._invite_visit_throttle_until, far_throttle)


class ThrottleActiveTests(unittest.TestCase):
    def test_inactive_when_unset(self):
        d = _make_dispatcher()
        self.assertFalse(d._invite_visit_throttle_active())

    def test_active_when_future(self):
        d = _make_dispatcher()
        d._invite_visit_throttle_until = datetime.utcnow() + timedelta(seconds=30)
        self.assertTrue(d._invite_visit_throttle_active())

    def test_inactive_when_expired(self):
        d = _make_dispatcher()
        d._invite_visit_throttle_until = datetime.utcnow() - timedelta(seconds=1)
        self.assertFalse(d._invite_visit_throttle_active())


if __name__ == "__main__":
    unittest.main()
