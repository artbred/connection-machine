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

# This module drives InviteTask.run() through the weekly-limit cooldown path,
# which calls send_notification. tests/__init__.py blanks these under discovery,
# but a direct `python tests/test_invite_state.py` run skips the package init —
# so blank them here too, or a real Telegram message goes out.
for _var in ("TELEGRAM_NOTIFICATIONS_URL", "TELEGRAM_CHAT_ID", "TELEGRAM_API_KEY"):
    os.environ[_var] = ""

from db import TaskType  # noqa: E402
from dispatcher import TaskDispatcher  # noqa: E402
from exceptions import TaskSkippedException  # noqa: E402
from invite_state import InviteStateStore  # noqa: E402
from metrics import NoopMetrics  # noqa: E402
from tasks.invite import InviteTask  # noqa: E402


class EmptyLocator:
    def count(self):
        return 0


class DummyPage:
    url = "https://www.linkedin.com/feed/"

    def locator(self, _selector: str):
        return EmptyLocator()


class SkippingInviteTask(InviteTask):
    def __init__(self, page, reason: str):
        super().__init__(page)
        self.reason = reason

    def send_connection_request(
        self,
        url: str,
        try_personal_message: bool = True,
    ) -> dict:
        raise TaskSkippedException(self.reason)


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
        self.assertEqual(
            snapshot["event_counts"][("skipped", "withdrawal_cooldown")], 1
        )

    def test_dispatcher_reuses_existing_invite_cooldown_without_extending_it(self):
        now = datetime.utcnow()
        with tempfile.TemporaryDirectory() as tmpdir:
            store = InviteStateStore(path=Path(tmpdir) / "invite_state.json")
            active_until = now + timedelta(days=2)
            store.set_cooldown(
                "weekly_limit_reached",
                active_until,
                source="invite_task",
                profile_url="https://example.com/profile",
            )

            dispatcher = TaskDispatcher.__new__(TaskDispatcher)
            dispatcher.invite_state = store
            dispatcher.next_execution_at = {}
            dispatcher.metrics = NoopMetrics()

            dispatcher.schedule_skip_cooldown(
                TaskType.SEND_INVITE,
                "weekly_limit_reached",
            )

            cooldown = store.get_active_cooldown(now=now)

        if cooldown is None:
            self.fail("cooldown should stay active")
        self.assertEqual(cooldown["active_until"], active_until)
        self.assertEqual(
            dispatcher.next_execution_at[TaskType.SEND_INVITE],
            active_until,
        )

    def test_verbose_weekly_reason_does_not_create_invite_cooldown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = InviteStateStore(path=Path(tmpdir) / "invite_state.json")
            task = SkippingInviteTask(
                DummyPage(),
                "A visible error message indicates the weekly connection limit has been reached.",
            )
            task.invite_state = store

            with self.assertRaises(TaskSkippedException) as raised:
                task.run({"url": "https://example.com/profile"})

            cooldown = store.get_active_cooldown()

        self.assertEqual(raised.exception.reason, "weekly_limit_reached")
        self.assertFalse(raised.exception.cooldown_eligible)
        self.assertIsNone(cooldown)

    def test_first_canonical_weekly_feedback_does_not_create_invite_cooldown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = InviteStateStore(path=Path(tmpdir) / "invite_state.json")
            task = SkippingInviteTask(DummyPage(), "weekly_limit_reached")
            task.invite_state = store

            with self.assertRaises(TaskSkippedException) as raised:
                task.run({"url": "https://example.com/profile"})

            cooldown = store.get_active_cooldown()

        self.assertEqual(raised.exception.reason, "weekly_limit_reached")
        self.assertFalse(raised.exception.cooldown_eligible)
        self.assertIsNone(cooldown)

    def test_repeated_weekly_feedback_creates_invite_cooldown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = InviteStateStore(path=Path(tmpdir) / "invite_state.json")
            store.record_event(
                outcome="skipped",
                reason="weekly_limit_reached",
                profile_url="https://example.com/previous",
                source="invite_task",
                recorded_at=datetime.utcnow(),
            )
            task = SkippingInviteTask(DummyPage(), "weekly_limit_reached")
            task.invite_state = store

            with self.assertRaises(TaskSkippedException) as raised:
                task.run({"url": "https://example.com/profile"})

            cooldown = store.get_active_cooldown()

        self.assertEqual(raised.exception.reason, "weekly_limit_reached")
        self.assertTrue(raised.exception.cooldown_eligible)
        self.assertIsNotNone(cooldown)


if __name__ == "__main__":
    unittest.main()
