import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from exceptions import TaskSkippedException  # noqa: E402
from tasks.invite import (  # noqa: E402
    InviteTask,
    _format_invite_notification,
    audience_filter_rejection,
    get_invite_audience_filter,
    parse_audience_stats,
)

# Live topcard text observed 2026-07-17 (andrewfelbinger)
REAL_TOPCARD = (
    "Andrew Felbinger\n· 2nd\nGrowth at Acme\n"
    "University of Pennsylvania - The Wharton School\n"
    "3,348 followers\n·\n500+ connections\nDennis and 3 other mutual connections"
)

# Live text observed 2026-07-17 on a sparse profile: no topcard followers line
# (followers only in the Activity header), connection count split across
# lines, and the "More profiles for you" module starting early.
REAL_SPARSE_PROFILE = (
    "Afnan Mohammed\n· 3rd\nAerospace Engineering Graduate\n"
    "Guzelyurt, Nicosia, Cyprus\n·\nContact info\n"
    "Middle East Technical University Northern Cyprus Campus\n"
    "49\n\nconnections\n\nMessage\nFollow\nActivity\n\n52 followers\n\nFollow\n"
    "Afnan Mohammed commented on a post\n•\n5mo\n"
    "This is a refreshing take on job hunting!\nShow all\n"
    "More profiles for you\nMohsin Akhtar\n· 3rd\nStudent"
)


class ParseAudienceStatsTest(unittest.TestCase):
    def test_real_topcard(self):
        stats = parse_audience_stats(REAL_TOPCARD)
        self.assertEqual(stats["followers"], 3348)
        self.assertEqual(stats["connections"], 500)
        self.assertTrue(stats["connections_capped"])

    def test_exact_connection_count_below_cap(self):
        stats = parse_audience_stats("Jane Doe\n342 connections")
        self.assertIsNone(stats["followers"])
        self.assertEqual(stats["connections"], 342)
        self.assertFalse(stats["connections_capped"])

    def test_abbreviated_follower_counts(self):
        self.assertEqual(
            parse_audience_stats("1.2K followers · 500+ connections")["followers"],
            1200,
        )
        self.assertEqual(parse_audience_stats("3M followers")["followers"], 3_000_000)

    def test_mutual_connection_phrase_is_not_a_count(self):
        stats = parse_audience_stats("Harry, Lucio and 1 other mutual connection")
        self.assertIsNone(stats["connections"])

    def test_missing_stats(self):
        stats = parse_audience_stats("A profile with no numbers at all")
        self.assertIsNone(stats["followers"])
        self.assertIsNone(stats["connections"])

    def test_stranger_counts_beyond_topcard_slice_ignored(self):
        text = "Jane Doe\nHeadline\n" + ("x" * 3100) + "\n12,895 followers"
        self.assertIsNone(parse_audience_stats(text)["followers"])

    def test_real_sparse_profile(self):
        stats = parse_audience_stats(REAL_SPARSE_PROFILE)
        self.assertEqual(stats["followers"], 52)
        self.assertEqual(stats["connections"], 49)
        self.assertFalse(stats["connections_capped"])
        self.assertIsNotNone(audience_filter_rejection(stats, 1000, True))

    def test_counts_after_foreign_module_heading_ignored(self):
        text = (
            "Jane Doe\nHeadline\nPages for you\nSpectro Cloud\n"
            "12,895 followers\nFollow"
        )
        stats = parse_audience_stats(text)
        self.assertIsNone(stats["followers"])

    def test_headline_follower_count_does_not_shadow_real_stat(self):
        text = (
            "Jane Grower\n· 2nd\nI help founders gain 100K followers on LinkedIn\n"
            "Acme University\n312 followers\n·\n104 connections"
        )
        stats = parse_audience_stats(text)
        self.assertEqual(stats["followers"], 312)
        self.assertEqual(stats["connections"], 104)

    def test_headline_connections_phrase_does_not_shadow_real_stat(self):
        text = (
            "Jane Grower\n· 2nd\nGet 500+ connections in 30 days\n"
            "42\n\nconnections"
        )
        stats = parse_audience_stats(text)
        self.assertEqual(stats["connections"], 42)
        self.assertFalse(stats["connections_capped"])

    def test_about_prose_follower_count_ignored(self):
        text = (
            "Afnan Mohammed\n· 3rd\nAerospace Engineering Graduate\n"
            "About\nI grew my TikTok to 120,000 followers in a year.\n"
            "Activity\n52 followers"
        )
        self.assertEqual(parse_audience_stats(text)["followers"], 52)

    def test_digit_ending_line_above_stat_does_not_merge(self):
        stats = parse_audience_stats("Université Paris 8\n1,234 followers")
        self.assertEqual(stats["followers"], 1234)
        stats = parse_audience_stats("École 42\n49\n\nconnections")
        self.assertEqual(stats["connections"], 49)

    def test_prose_containing_promoted_does_not_wipe_stats(self):
        text = (
            "Dana K\n· 2nd\nRecently promoted to VP of Sales\nAustin\n"
            "4,120 followers\n·\n500+ connections"
        )
        stats = parse_audience_stats(text)
        self.assertEqual(stats["followers"], 4120)
        self.assertTrue(stats["connections_capped"])

    def test_multiline_stat_rendering(self):
        text = "Eric Melillo\n20,683 followers\n\n·\n\n500+\n\nconnections"
        stats = parse_audience_stats(text)
        self.assertEqual(stats["followers"], 20683)
        self.assertEqual(stats["connections"], 500)
        self.assertTrue(stats["connections_capped"])

    def test_single_line_combined_stats(self):
        stats = parse_audience_stats("3,348 followers · 500+ connections")
        self.assertEqual(stats["followers"], 3348)
        self.assertEqual(stats["connections"], 500)
        self.assertTrue(stats["connections_capped"])


class AudienceFilterRejectionTest(unittest.TestCase):
    PASSING = {"followers": 3348, "connections": 500, "connections_capped": True}

    def test_disabled_filter_passes_everything(self):
        self.assertIsNone(
            audience_filter_rejection({"followers": None, "connections": None}, 0, False)
        )

    def test_passing_profile(self):
        self.assertIsNone(audience_filter_rejection(self.PASSING, 1000, True))

    def test_too_few_followers(self):
        stats = dict(self.PASSING, followers=999)
        self.assertIn("999 followers", audience_filter_rejection(stats, 1000, True))

    def test_under_500_connections(self):
        stats = dict(self.PASSING, connections=342, connections_capped=False)
        self.assertIn("342 connections", audience_filter_rejection(stats, 1000, True))

    def test_fails_closed_on_missing_followers(self):
        stats = dict(self.PASSING, followers=None)
        self.assertIsNotNone(audience_filter_rejection(stats, 1000, False))

    def test_fails_closed_on_missing_connections(self):
        stats = dict(self.PASSING, connections=None)
        self.assertIsNotNone(audience_filter_rejection(stats, 0, True))

    def test_followers_only_filter_ignores_connections(self):
        stats = {"followers": 5000, "connections": None, "connections_capped": False}
        self.assertIsNone(audience_filter_rejection(stats, 1000, False))


class EnvConfigTest(unittest.TestCase):
    def test_defaults_disabled(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVITE_MIN_FOLLOWERS", None)
            os.environ.pop("INVITE_REQUIRE_500_CONNECTIONS", None)
            self.assertEqual(get_invite_audience_filter(), (0, False))

    def test_configured_values(self):
        with patch.dict(
            os.environ,
            {"INVITE_MIN_FOLLOWERS": "1,000", "INVITE_REQUIRE_500_CONNECTIONS": "true"},
        ):
            self.assertEqual(get_invite_audience_filter(), (1000, True))

    def test_invalid_int_treated_as_disabled(self):
        with patch.dict(os.environ, {"INVITE_MIN_FOLLOWERS": "lots"}):
            self.assertEqual(get_invite_audience_filter()[0], 0)


class EnforceAudienceFilterTest(unittest.TestCase):
    def _make_task(self, page_text):
        class FakePage:
            def evaluate(self, script, *args):
                return page_text

        class FakeHuman:
            def random_sleep(self, *args, **kwargs):
                return None

        task = InviteTask.__new__(InviteTask)
        task.page = FakePage()
        task.human = FakeHuman()
        return task

    def test_disabled_filter_returns_none_without_reading_page(self):
        task = self._make_task(REAL_TOPCARD)
        with patch.dict(
            os.environ,
            {"INVITE_MIN_FOLLOWERS": "0", "INVITE_REQUIRE_500_CONNECTIONS": "false"},
        ):
            self.assertIsNone(task._enforce_audience_filter("https://x/in/a/"))

    def test_passing_profile_returns_stats(self):
        task = self._make_task(REAL_TOPCARD)
        with patch.dict(
            os.environ,
            {"INVITE_MIN_FOLLOWERS": "1000", "INVITE_REQUIRE_500_CONNECTIONS": "true"},
        ):
            stats = task._enforce_audience_filter("https://x/in/a/")
        self.assertEqual(stats["followers"], 3348)
        self.assertTrue(stats["connections_capped"])

    def test_failing_profile_raises_audience_filter_skip(self):
        task = self._make_task("Jane Doe\n120 followers\n·\n89 connections")
        with patch.dict(
            os.environ,
            {"INVITE_MIN_FOLLOWERS": "1000", "INVITE_REQUIRE_500_CONNECTIONS": "true"},
        ):
            with self.assertRaises(TaskSkippedException) as ctx:
                task._enforce_audience_filter("https://x/in/a/")
        self.assertEqual(ctx.exception.reason, "audience_filter")
        self.assertFalse(ctx.exception.cooldown_eligible)


class RetryLoopTest(unittest.TestCase):
    """The stat retry loop must wait for the CONFIGURED stat, not any stat."""

    def _make_task(self, page_texts):
        texts = iter(page_texts)
        calls = {"n": 0}

        class FakePage:
            def evaluate(self, script, *args):
                calls["n"] += 1
                try:
                    return next(texts)
                except StopIteration:
                    return page_texts[-1]

        class FakeHuman:
            def random_sleep(self, *args, **kwargs):
                return None

        task = InviteTask.__new__(InviteTask)
        task.page = FakePage()
        task.human = FakeHuman()
        return task, calls

    def test_followers_only_waits_past_a_connections_only_snapshot(self):
        # Snapshot 1: topcard connections present, followers not yet hydrated.
        # Snapshot 2: followers streamed in. A followers-only filter must not
        # settle on snapshot 1 and fail closed.
        snapshots = [
            "Jane Doe\n· 2nd\n49\n\nconnections",
            "Jane Doe\n· 2nd\n1,200 followers\n·\n49\n\nconnections",
        ]
        task, calls = self._make_task(snapshots)
        with patch.dict(
            os.environ,
            {"INVITE_MIN_FOLLOWERS": "1000", "INVITE_REQUIRE_500_CONNECTIONS": "false"},
        ):
            stats = task._enforce_audience_filter("https://x/in/a/")
        self.assertEqual(stats["followers"], 1200)
        self.assertGreaterEqual(calls["n"], 2)


class SendConnectionRequestWiringTest(unittest.TestCase):
    """The filter must actually run inside the invite flow and short-circuit it."""

    def _make_task(self):
        class FakePage:
            url = "https://www.linkedin.com/in/a/"

            def goto(self, *a, **k):
                return None

            def wait_for_selector(self, *a, **k):
                return None

        class FakeHuman:
            def random_sleep(self, *args, **kwargs):
                return None

        task = InviteTask.__new__(InviteTask)
        task.page = FakePage()
        task.human = FakeHuman()
        return task

    def test_filter_runs_before_content_capture_and_skips_person(self):
        import tasks.invite as invite_mod
        from connection_state import ConnectionState

        task = self._make_task()
        order = []

        def fake_filter(url):
            order.append("filter")
            raise TaskSkippedException("audience_filter", cooldown_eligible=False)

        def fake_capture():
            order.append("capture")
            return "", ""

        task._enforce_audience_filter = fake_filter
        task.get_profile_content = fake_capture

        with patch.object(
            invite_mod, "detect_connection_state", return_value=ConnectionState.CONNECTABLE
        ), patch.object(invite_mod, "try_heuristic_connect", return_value=False):
            with self.assertRaises(TaskSkippedException) as ctx:
                task.send_connection_request("https://www.linkedin.com/in/a/", True)

        self.assertEqual(ctx.exception.reason, "audience_filter")
        # Filter ran; content capture never happened → person skipped entirely.
        self.assertEqual(order, ["filter"])

    def test_confirmed_notification_carries_filter_stats(self):
        import tasks.invite as invite_mod
        from connection_state import ConnectionState

        task = InviteTask.__new__(InviteTask)
        task._last_audience_stats = {
            "followers": 3348,
            "connections": 500,
            "connections_capped": True,
        }
        task._record_invite_history = lambda *a, **k: None

        class FakeState:
            def record_event(self, *a, **k):
                return None

        task.invite_state = FakeState()

        captured = {}
        with patch.object(
            invite_mod,
            "send_notification",
            side_effect=lambda text: captured.setdefault("text", text),
        ):
            task._record_confirmed_invite(
                "https://x/in/a/", ConnectionState.PENDING, "hello"
            )

        self.assertIn("Followers: 3,348", captured["text"])
        self.assertIn("Connections: 500+", captured["text"])


class NotificationStatsTest(unittest.TestCase):
    def test_notification_includes_stats(self):
        text = _format_invite_notification(
            "Invite confirmed",
            profile_url="https://x/in/a/",
            state="pending",
            message="hello",
            audience_stats={
                "followers": 3348,
                "connections": 500,
                "connections_capped": True,
            },
        )
        self.assertIn("Followers: 3,348", text)
        self.assertIn("Connections: 500+", text)

    def test_notification_without_stats_unchanged(self):
        text = _format_invite_notification(
            "Invite confirmed", profile_url="https://x/in/a/", state="pending"
        )
        self.assertNotIn("Followers", text)
        self.assertNotIn("Connections", text)


if __name__ == "__main__":
    unittest.main()
