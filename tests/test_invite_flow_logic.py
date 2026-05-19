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

from connection_state import ConnectionState, resolve_connection_state  # noqa: E402
from db import TaskType  # noqa: E402
from dispatcher import (  # noqa: E402
    build_cooldown_notification,
    normalize_skip_reason,
    remaining_minutes,
)
from tasks.invite import _format_invite_notification, classify_invitation_feedback  # noqa: E402
from tasks.invite import classify_platform_invitation_feedback  # noqa: E402
from tasks.invite import _locator_matches_expected_text  # noqa: E402


class FakeLocator:
    def __init__(self, text: str = "", aria_label: str = ""):
        self.text = text
        self.aria_label = aria_label

    def inner_text(self, timeout: int = 300):
        return self.text

    def get_attribute(self, name: str):
        if name == "aria-label":
            return self.aria_label
        return None


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

    def test_exact_linkedin_weekly_limit_message_is_detected(self):
        text = (
            "Your invitation to Jacob was not sent because you have reached the "
            "weekly limit for connection invitations. Please try again next week"
        )
        self.assertEqual(
            classify_invitation_feedback(text),
            "weekly_limit_reached",
        )

    def test_strict_platform_feedback_requires_send_failure_context(self):
        text = "Weekly connection limit tips can help you connect safely."

        self.assertIsNone(classify_platform_invitation_feedback(text))

    def test_strict_platform_feedback_detects_linkedin_weekly_limit(self):
        text = (
            "Your invitation to Jacob was not sent because you have reached the "
            "weekly limit for connection invitations. Please try again next week"
        )

        self.assertEqual(
            classify_platform_invitation_feedback(text),
            "weekly_limit_reached",
        )

    def test_verbose_weekly_limit_reason_is_normalized_for_cooldowns(self):
        reason = (
            "A visible error message indicates the weekly connection limit has "
            "been reached, preventing further connection requests."
        )
        self.assertEqual(
            normalize_skip_reason(TaskType.SEND_INVITE, reason),
            "weekly_limit_reached",
        )

    def test_profile_not_found_reason_is_canonicalized(self):
        reason = "The page is a 404 error page ('This page doesn’t exist')."
        self.assertEqual(
            normalize_skip_reason(TaskType.SEND_INVITE, reason),
            "profile_not_found",
        )

    def test_pending_reason_is_canonicalized(self):
        reason = "Connection is already pending as indicated by the 'Pending' button on the profile."
        self.assertEqual(
            normalize_skip_reason(TaskType.SEND_INVITE, reason),
            "already_pending",
        )

    def test_slavic_policy_reason_is_canonicalized(self):
        reason = (
            "The person's name (Nikolay Seleznev) is Slavic, and the instructions "
            "specify not to connect with such individuals."
        )
        self.assertEqual(
            normalize_skip_reason(TaskType.SEND_INVITE, reason),
            "policy_skip",
        )

    def test_connect_unavailable_reason_is_canonicalized(self):
        reason = (
            "The 'More' dropdown is currently open, but it does not contain a "
            "'Connect' option in the visible list or the provided HTML section."
        )
        self.assertEqual(
            normalize_skip_reason(TaskType.SEND_INVITE, reason),
            "connect_unavailable",
        )

    def test_navigation_timeout_is_canonicalized(self):
        reason = (
            "Page.goto: Timeout 60000ms exceeded.\nCall log:\n"
            '  - navigating to "https://www.linkedin.com/in/example", '
            'waiting until "domcontentloaded"'
        )
        self.assertEqual(
            normalize_skip_reason(TaskType.SEND_INVITE, reason),
            "navigation_timeout",
        )

    def test_send_button_timeout_is_canonicalized(self):
        reason = (
            "Locator.click: Timeout 30000ms exceeded.\nCall log:\n"
            "  - waiting for locator(\"button[aria-label='Send invitation']\").first"
        )
        self.assertEqual(
            normalize_skip_reason(TaskType.SEND_INVITE, reason),
            "send_button_timeout",
        )

    def test_add_note_failure_is_canonicalized(self):
        reason = "Could not reach 'Add a note' after 5 iterations"
        self.assertEqual(
            normalize_skip_reason(TaskType.SEND_INVITE, reason),
            "add_note_unreachable",
        )

    def test_withdraw_feedback_is_detected(self):
        text = (
            "Invitation not sent because you're still withdrawing previous invitations."
        )
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

    def test_invite_notification_escapes_dynamic_fields(self):
        message = _format_invite_notification(
            "Invite confirmed",
            profile_url="https://example.com/profile?x=1&y=2",
            state="pending",
            message="<b>Hello</b> & welcome",
        )
        self.assertIn("https://example.com/profile?x=1&amp;y=2", message)
        self.assertIn("&lt;b&gt;Hello&lt;/b&gt; &amp; welcome", message)

    def test_weekly_limit_notification_mentions_7_day_cooldown(self):
        next_allowed = datetime(2026, 4, 6, 12, 30)
        message = build_cooldown_notification(
            TaskType.SEND_INVITE,
            "A visible error message indicates the weekly invitation limit has been reached.",
            next_allowed,
        )
        self.assertIsNotNone(message)
        if message is None:
            self.fail("weekly limit notification should be generated")
        self.assertIn("Cooldown: 7 days", message)
        self.assertIn("2026-04-06 12:30 UTC", message)

    def test_remaining_minutes_uses_total_seconds_for_multi_day_deltas(self):
        self.assertEqual(
            remaining_minutes(timedelta(days=5, hours=5, minutes=38)), 7538
        )

    def test_icon_only_more_button_matches_expected_text_from_aria_label(self):
        locator = FakeLocator(text="", aria_label="More")

        self.assertTrue(_locator_matches_expected_text(locator, "More"))

    def test_aria_label_with_name_matches_generic_connect_expected_text(self):
        locator = FakeLocator(text="", aria_label="Connect with Ada Lovelace")

        self.assertTrue(_locator_matches_expected_text(locator, "Connect"))


if __name__ == "__main__":
    unittest.main()
