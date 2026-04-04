import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from connection_state import ConnectionState, resolve_connection_state
from db import TaskType
from dispatcher import build_cooldown_notification
from tasks.invite import classify_invitation_feedback


class InviteFlowLogicTests(unittest.TestCase):
    def test_character_limit_alert_is_not_weekly_limit(self):
        text = "Add a note to your invitation. Limit 200 characters."
        self.assertIsNone(classify_invitation_feedback(text))

    def test_weekly_limit_feedback_is_detected(self):
        text = "Invitation not sent. You've reached the weekly invitation limit."
        self.assertEqual(
            classify_invitation_feedback(text),
            "weekly_limit_reached",
        )

    def test_withdraw_feedback_is_detected(self):
        text = "Invitation not sent because you're still withdrawing previous invitations."
        self.assertEqual(
            classify_invitation_feedback(text),
            "withdrawal_cooldown",
        )

    def test_pending_state_wins_over_connectable(self):
        state = resolve_connection_state(
            has_pending=True,
            has_connected_marker=False,
            has_connect=True,
            has_following=False,
        )
        self.assertEqual(state, ConnectionState.PENDING)

    def test_connected_state_wins_over_connectable(self):
        state = resolve_connection_state(
            has_pending=False,
            has_connected_marker=True,
            has_connect=True,
            has_following=False,
        )
        self.assertEqual(state, ConnectionState.CONNECTED)

    def test_weekly_limit_notification_mentions_24_hour_cooldown(self):
        next_allowed = datetime(2026, 4, 6, 12, 30)
        message = build_cooldown_notification(
            TaskType.SEND_INVITE,
            "weekly_limit_reached",
            next_allowed,
        )
        self.assertIsNotNone(message)
        self.assertIn("Cooldown: 24 hours", message)
        self.assertIn("2026-04-06 12:30 UTC", message)


if __name__ == "__main__":
    unittest.main()
