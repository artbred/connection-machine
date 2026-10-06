import sys
import unittest
from bisect import bisect_left
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from invite_schedule import next_invite_time  # noqa: E402


class InviteScheduleTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 6, 12)
        self.window = timedelta(hours=24)
        self.slot = self.window / 10
        self.minimum_gap = timedelta(minutes=100, seconds=48)

    def test_empty_history_is_immediately_ready(self):
        self.assertEqual(next_invite_time([], self.now), self.now)

    def test_full_quota_waits_for_oldest_success_to_leave_window(self):
        history = [self.now - timedelta(hours=23 - 2 * i) for i in range(10)]
        self.assertEqual(
            next_invite_time(history, self.now),
            self.now + timedelta(hours=1, microseconds=1),
        )

    def test_exact_twenty_four_hour_boundary_still_consumes_a_slot(self):
        history = [
            self.now - self.window + timedelta(hours=offset)
            for offset in (0, 4, 6, 8, 10, 12, 14, 16, 18, 21)
        ]
        self.assertEqual(
            next_invite_time(history, self.now),
            self.now + timedelta(microseconds=1),
        )
        after_boundary = self.now + timedelta(microseconds=1)
        self.assertEqual(next_invite_time(history, after_boundary), after_boundary)

    def test_overfull_history_uses_tenth_most_recent_success(self):
        history = [self.now - timedelta(hours=23 - i) for i in range(12)]
        self.assertEqual(
            next_invite_time(list(reversed(history)), self.now),
            self.now + timedelta(hours=3, microseconds=1),
        )

    def test_minimum_gap_bounds_catchup(self):
        history = [self.now - timedelta(hours=20), self.now]
        self.assertEqual(
            next_invite_time(history, self.now), self.now + self.minimum_gap
        )

    def test_full_quota_also_preserves_minimum_gap(self):
        history = [self.now - self.window] * 9 + [self.now]
        self.assertEqual(
            next_invite_time(history, self.now), self.now + self.minimum_gap
        )

    def test_action_and_rejection_overhead_does_not_restart_full_slot(self):
        first_success = self.now
        second_success = first_success + self.slot + timedelta(minutes=26)
        deadline = next_invite_time([first_success, second_success], second_success)
        self.assertLess(deadline, second_success + self.slot)
        self.assertGreaterEqual(deadline - second_success, self.minimum_gap)

    def test_old_events_are_ignored_and_deadline_never_precedes_now(self):
        old = self.now - self.window - timedelta(microseconds=1)
        self.assertEqual(next_invite_time([old] * 10, self.now), self.now)
        recent = self.now - timedelta(hours=12)
        self.assertEqual(next_invite_time([old, recent], self.now), self.now)

    def test_future_successes_are_retained_conservatively(self):
        future = self.now + timedelta(hours=1)
        self.assertGreaterEqual(
            next_invite_time([future], self.now), future + self.minimum_gap
        )
        self.assertEqual(
            next_invite_time([future] * 10, self.now),
            future + self.window + timedelta(microseconds=1),
        )

    def test_duplicate_timestamps_each_consume_a_slot(self):
        self.assertEqual(
            next_invite_time([self.now] * 10, self.now),
            self.now + self.window + timedelta(microseconds=1),
        )

    def test_deadline_is_stable_across_polls_and_persisted_restart(self):
        history = [
            self.now - timedelta(hours=4, minutes=30),
            self.now - timedelta(minutes=90),
        ]
        original = history.copy()
        expected = next_invite_time(history, self.now)
        restored = [
            datetime.fromisoformat(timestamp.isoformat()) for timestamp in history
        ]
        self.assertEqual(next_invite_time(restored, self.now), expected)
        self.assertEqual(
            next_invite_time(restored, self.now + timedelta(minutes=5)), expected
        )
        self.assertEqual(history, original)

    def test_custom_quota_controls_spacing(self):
        self.assertEqual(
            next_invite_time(
                [self.now - timedelta(hours=20), self.now], self.now, limit=4
            ),
            self.now + timedelta(hours=6) * 0.7,
        )
        self.assertEqual(
            next_invite_time([self.now], self.now, limit=1),
            self.now + self.window + timedelta(microseconds=1),
        )

    def test_invalid_limits_are_rejected_even_without_history(self):
        for limit in (0, -1):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                next_invite_time([], self.now, limit=limit)
        for limit in (True, False, 2.5, "10", None):
            with self.subTest(limit=limit), self.assertRaises(TypeError):
                next_invite_time([], self.now, limit=limit)

    def test_long_horizon_absorbs_runtime_without_exceeding_rolling_cap(self):
        horizon = self.now + timedelta(days=120)
        warmup_end = self.now + timedelta(days=7)
        completions = []
        now = self.now
        while now < horizon:
            start = next_invite_time(completions, now)
            # Some slots first reject a candidate; neither rejection nor action
            # runtime counts as a successful send, but both consume real time.
            rejected_preflight = timedelta(
                minutes=18 if len(completions) % 7 == 0 else 0
            )
            completed = start + rejected_preflight + timedelta(minutes=12)
            if completed >= horizon:
                break
            if completions:
                self.assertGreaterEqual(start - completions[-1], self.minimum_gap)
            self.assertLess(
                len(completions) - bisect_left(completions, start - self.window), 10
            )
            completions.append(completed)
            self.assertLessEqual(
                len(completions) - bisect_left(completions, completed - self.window), 10
            )
            now = completed

        steady_successes = len(completions) - bisect_left(completions, warmup_end)
        steady_days = (horizon - warmup_end) / self.window
        self.assertGreaterEqual(steady_successes / steady_days, 9.8)
        self.assertLessEqual(steady_successes / steady_days, 10)


if __name__ == "__main__":
    unittest.main()
