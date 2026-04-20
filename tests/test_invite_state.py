import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from invite_state import InviteStateStore


class InviteStateStoreTests(unittest.TestCase):
    def test_recent_events_are_sorted_and_counted(self):
        now = datetime.utcnow()
        with tempfile.TemporaryDirectory() as tmpdir:
            store = InviteStateStore(path=Path(tmpdir) / "invite_state.json")
            store.record_event(
                outcome="failed",
                reason="invite_not_confirmed",
                profile_url="https://example.com/failed",
                source="invite_task",
                recorded_at=now - timedelta(hours=1),
            )
            store.record_event(
                outcome="success",
                reason="",
                profile_url="https://example.com/success",
                source="invite_task",
                status="pending",
                recorded_at=now - timedelta(minutes=5),
            )

            recent_events = store.get_recent_events(limit=5)
            event_counts = store.get_event_counts()
            reason_counts = store.get_reason_counts()

        self.assertEqual(
            [event["profile_url"] for event in recent_events],
            ["https://example.com/success", "https://example.com/failed"],
        )
        self.assertEqual(event_counts[("success", "")], 1)
        self.assertEqual(event_counts[("failed", "invite_not_confirmed")], 1)
        self.assertEqual(reason_counts["invite_not_confirmed"], 1)

    def test_expired_cooldown_is_cleared(self):
        now = datetime.utcnow()
        with tempfile.TemporaryDirectory() as tmpdir:
            store = InviteStateStore(path=Path(tmpdir) / "invite_state.json")
            store.set_cooldown(
                "weekly_limit_reached",
                now - timedelta(minutes=1),
                source="invite_task",
                profile_url="https://example.com/profile",
            )

            self.assertIsNone(store.get_active_cooldown(now=now))
            self.assertIsNone(store.load()["cooldown"])

    def test_metrics_snapshot_includes_active_cooldown(self):
        now = datetime.utcnow()
        with tempfile.TemporaryDirectory() as tmpdir:
            store = InviteStateStore(path=Path(tmpdir) / "invite_state.json")
            active_until = now + timedelta(hours=6)
            store.set_cooldown(
                "withdrawal_cooldown",
                active_until,
                source="dispatcher",
                profile_url="https://example.com/profile",
            )
            store.record_event(
                outcome="skipped",
                reason="withdrawal_cooldown",
                profile_url="https://example.com/profile",
                source="invite_task",
                cooldown_until=active_until,
                recorded_at=now,
            )

            snapshot = store.get_metrics_snapshot(recent_limit=5)

        self.assertEqual(snapshot["cooldown"]["reason"], "withdrawal_cooldown")
        self.assertEqual(snapshot["last_event"]["reason"], "withdrawal_cooldown")
        self.assertEqual(snapshot["event_counts"][("skipped", "withdrawal_cooldown")], 1)


if __name__ == "__main__":
    unittest.main()
