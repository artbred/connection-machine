import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from exceptions import TaskSkippedException  # noqa: E402
from tasks.invite import (  # noqa: E402
    InviteTask,
    audience_filter_rejection,
    get_invite_audience_filter,
    parse_audience_stats,
)
from linkedin_profile import wait_for_profile  # noqa: E402
from tests.browser_case import OfflineBrowserTestCase  # noqa: E402

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
            "Jane Doe\nHeadline\nPages for you\nSpectro Cloud\n12,895 followers\nFollow"
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
        text = "Jane Grower\n· 2nd\nGet 500+ connections in 30 days\n42\n\nconnections"
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
            audience_filter_rejection(
                {"followers": None, "connections": None}, 0, False
            )
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


class EnforceAudienceFilterTest(OfflineBrowserTestCase):
    def _make_task(self, counts: str, extra: str = ""):
        self.load_html(
            '<main><section class="pv-top-card">'
            '<h1><a href="/in/jane-prospect/">Jane Prospect</a></h1>'
            f'<div id="counts">{counts}</div>'
            "<button>Connect</button></section>"
            f"{extra}</main>"
        )
        task = InviteTask.__new__(InviteTask)
        task.page = self.page
        task.human = SimpleNamespace(
            random_sleep=lambda *_: self.page.wait_for_timeout(100)
        )
        task._target_identity = wait_for_profile(
            self.page, self.page.url, personalize=False
        ).identity
        return task

    def test_qualified_target_passes(self):
        task = self._make_task("3,348 followers<br>500+ connections")
        with patch.dict(
            os.environ,
            {"INVITE_MIN_FOLLOWERS": "3000", "INVITE_REQUIRE_500_CONNECTIONS": "true"},
        ):
            stats = task._enforce_audience_filter(self.page.url)
        self.assertEqual(stats["followers"], 3348)
        self.assertEqual(stats["connections"], 500)

    def test_foreign_counts_cannot_qualify_small_target(self):
        task = self._make_task(
            "120 followers<br>89 connections",
            '<div><div role="heading">More profiles for you</div>'
            '<a href="/in/stranger/">Stranger</a>'
            "<div>20,000 followers<br>500+ connections</div></div>",
        )
        with (
            patch.dict(
                os.environ,
                {
                    "INVITE_MIN_FOLLOWERS": "3000",
                    "INVITE_REQUIRE_500_CONNECTIONS": "true",
                },
            ),
            self.assertRaises(TaskSkippedException) as caught,
        ):
            task._enforce_audience_filter(self.page.url)
        self.assertEqual(caught.exception.reason, "audience_filter")
        self.assertFalse(caught.exception.cooldown_eligible)

    def test_unreadable_counts_are_not_labeled_below_threshold(self):
        task = self._make_task(
            "500+ connections",
            "<section><h2>Activity</h2><div>20,000 followers</div></section>",
        )
        with (
            patch.dict(
                os.environ,
                {
                    "INVITE_MIN_FOLLOWERS": "3000",
                    "INVITE_REQUIRE_500_CONNECTIONS": "true",
                },
            ),
            self.assertRaises(TaskSkippedException) as caught,
        ):
            task._enforce_audience_filter(self.page.url)
        self.assertEqual(caught.exception.reason, "audience_unavailable")

    def test_waits_for_configured_stat_to_hydrate(self):
        task = self._make_task("49 connections")
        self.page.evaluate("""() => setTimeout(() => {
            document.querySelector('#counts').innerHTML =
                '3,200 followers<br>49 connections';
        }, 150)""")
        with patch.dict(
            os.environ,
            {"INVITE_MIN_FOLLOWERS": "3000", "INVITE_REQUIRE_500_CONNECTIONS": "false"},
        ):
            stats = task._enforce_audience_filter(self.page.url)
        self.assertEqual(stats["followers"], 3200)
        self.assertEqual(stats["connections"], 49)

    def test_profile_drift_aborts_the_filter(self):
        task = self._make_task("3,348 followers<br>500+ connections")
        self.page.locator("h1").evaluate(
            "el => el.innerHTML = '<a href=\"/in/stranger/\">Stranger</a>'"
        )
        with (
            patch.dict(
                os.environ,
                {
                    "INVITE_MIN_FOLLOWERS": "3000",
                    "INVITE_REQUIRE_500_CONNECTIONS": "true",
                },
            ),
            self.assertRaises(TaskSkippedException) as caught,
        ):
            task._enforce_audience_filter(self.page.url)
        self.assertEqual(caught.exception.reason, "profile_identity_mismatch")


if __name__ == "__main__":
    unittest.main()
