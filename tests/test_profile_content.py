"""Canonical identity and bounded profile hydration behavior, without live access."""

import time
import unittest
from dataclasses import FrozenInstanceError

from tests.browser_case import OfflineBrowserTestCase
from exceptions import TaskSkippedException
from linkedin_profile import (
    ProfileIdentity,
    assert_profile_identity,
    canonical_profile_url,
    get_profile_topcard,
    read_profile_snapshot,
    wait_for_profile,
)

URL = "https://www.linkedin.com/in/jane-prospect/"
ABOUT = (
    "I design streaming pipelines and event-driven systems for retail inventory teams. "
    "My background includes database operations, schema design, and analytics observability. "
    "I lead technical delivery across our platform teams."
)
EXPERIENCE = (
    "Staff engineer at Orchard Analytics. Built a reliable inventory ingestion platform, "
    "mentored the data engineering team, and delivered resilient processing services. "
    "I also led production reliability improvements."
)
TOPCARD = f"""<section class="pv-top-card">
<a href="{URL}"><h1>Jane Prospect</h1></a>
<div class="text-body-medium">Building data platforms at Orchard Analytics</div>
<div>5,000 followers · 500+ connections</div>
<a href="{URL}overlay/contact-info/">Contact info</a>
<button id="connect">Connect</button></section>"""
SECTIONS = f"""<section id="about"><h2>About</h2><p>{ABOUT}</p></section>
<section id="experience"><h2>Experience</h2><p>{EXPERIENCE}</p></section>"""


def profile_html(sections=SECTIONS, topcard=TOPCARD, extra=""):
    return f"<!doctype html><html><body><main>{topcard}{sections}{extra}</main></body></html>"


class CanonicalProfileUrlTests(unittest.TestCase):
    def test_normalizes_real_linkedin_hosts_and_encoding(self):
        cases = {
            "http://linkedin.com/in/Jane-Prospect": URL,
            "https://uk.linkedin.com/in/jane-prospect/?trk=abc#about": URL,
            "https://WWW.LINKEDIN.COM/in/%6Aane-prospect/": URL,
            "https://www.linkedin.com/in/Andr%C3%A9/": "https://www.linkedin.com/in/andr%C3%A9/",
            "https://linkedin.com/in/Andre%CC%81/": "https://www.linkedin.com/in/andr%C3%A9/",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(canonical_profile_url(value), expected)

    def test_rejects_deceptive_hosts_paths_and_slug_boundaries(self):
        for value in (
            "https://linkedin.com.evil.test/in/jane-prospect/",
            "https://notlinkedin.com/in/jane-prospect/",
            "https://linkedin.com@evil.test/in/jane-prospect/",
            "https://evil@linkedin.com/in/jane-prospect/",
            "https://www.linkedin.com/in/jane-prospect/details/",
            "https://www.linkedin.com/in/jane-prospect%2Fother/",
            "https://www.linkedin.com/in/jane-prospect%252Fother/",
            "https://www.linkedin.com/in/jane-prospect%3Fother/",
            "https://www.linkedin.com/in/jane-prospect%5Cother/",
            "https://www.linkedin.com/in/%ZZ/",
            "https://www.linkedin.com/in/%FF/",
            "https://www.linkedin.com/in/%2E%2E/",
            "https://www.linkedin.com/in//",
            "https://www.linkedin.com/in/jane-prospect//",
            "https://www.linkedin.com:8888/in/jane-prospect/",
            "https://www.linkedin.com/in/jane\n-prospect/",
            "javascript:alert(1)",
            "//www.linkedin.com/in/jane-prospect/",
            "https://www.linkedin.com/company/jane-prospect/",
        ):
            with self.subTest(value=value):
                self.assertIsNone(canonical_profile_url(value))

    def test_identity_is_immutable(self):
        identity = ProfileIdentity(URL, "jane-prospect", "Jane Prospect")
        with self.assertRaises(FrozenInstanceError):
            identity.name = "Other Person"


class ProfileIdentityBrowserTests(OfflineBrowserTestCase):
    def assert_skipped(self, reason, **kwargs):
        with self.assertRaises(TaskSkippedException) as raised:
            wait_for_profile(self.page, URL, timeout_ms=250, **kwargs)
        self.assertEqual(raised.exception.reason, reason)
        self.assertFalse(raised.exception.cooldown_eligible)

    def test_identity_and_topcard_are_verified(self):
        self.load_html(profile_html())
        snapshot = wait_for_profile(self.page, URL, personalize=False)
        self.assertEqual(
            snapshot.identity, ProfileIdentity(URL, "jane-prospect", "Jane Prospect")
        )
        self.assertEqual(
            get_profile_topcard(self.page, snapshot.identity).get_attribute("class"),
            "pv-top-card",
        )
        self.assertEqual(
            assert_profile_identity(self.page, snapshot.identity), snapshot
        )

    def test_canonical_alone_cannot_establish_current_topcard(self):
        topcard = "<section><h1>Old Person</h1><div>Old headline</div><button>Connect</button></section>"
        self.load_html(
            f'<link rel="canonical" href="{URL}">' + profile_html(topcard=topcard)
        )
        self.assertIsNone(read_profile_snapshot(self.page, URL))
        self.assert_skipped("profile_not_ready")

    def test_stale_topcard_rejects_even_with_current_canonical(self):
        stale = TOPCARD.replace("jane-prospect", "old-person").replace(
            "Jane Prospect", "Old Person"
        )
        self.load_html(
            f'<link rel="canonical" href="{URL}">' + profile_html(topcard=stale)
        )
        self.assert_skipped("profile_identity_mismatch")

    def test_contradictory_contact_link_rejects(self):
        topcard = TOPCARD.replace(
            f"{URL}overlay/contact-info/",
            "https://www.linkedin.com/in/other/overlay/contact-info/",
        )
        self.load_html(profile_html(topcard=topcard))
        self.assert_skipped("profile_identity_mismatch")

    def test_contradictory_canonical_rejects(self):
        self.load_html(
            '<link rel="canonical" href="https://www.linkedin.com/in/other/">'
            + profile_html()
        )
        self.assert_skipped("profile_identity_mismatch")

    def test_actual_url_slug_prefix_is_not_identity(self):
        self.load_html(
            profile_html(), URL.replace("jane-prospect/", "jane-prospect-extra/")
        )
        self.assert_skipped("profile_identity_mismatch")

    def test_multiple_topcards_reject(self):
        other = TOPCARD.replace("jane-prospect", "other").replace(
            "Jane Prospect", "Other Person"
        )
        self.load_html(profile_html(topcard=TOPCARD + other))
        self.assert_skipped("profile_identity_mismatch")

    def test_multiple_equal_topcards_are_still_ambiguous(self):
        self.load_html(profile_html(topcard=TOPCARD + TOPCARD))
        self.assert_skipped("profile_identity_mismatch")

    def test_sidebar_identity_does_not_prove_primary_owner(self):
        self.load_html(profile_html(topcard="", extra=f"<aside>{TOPCARD}</aside>"))
        self.assert_skipped("profile_not_ready")

    def test_name_change_invalidates_previously_verified_identity(self):
        self.load_html(profile_html())
        identity = wait_for_profile(self.page, URL, personalize=False).identity
        self.page.locator("h1").evaluate("el => el.textContent = 'Other Person'")
        with self.assertRaises(TaskSkippedException) as raised:
            assert_profile_identity(self.page, identity)
        self.assertEqual(raised.exception.reason, "profile_identity_mismatch")

    def test_invitation_overlay_does_not_erase_underlying_identity(self):
        self.load_html(profile_html())
        identity = wait_for_profile(self.page, URL, personalize=False).identity
        self.page.evaluate("""() => {
          document.querySelector('main').setAttribute('aria-hidden', 'true');
          document.querySelector('main').setAttribute('inert', '');
          const dialog = document.createElement('div'); dialog.setAttribute('role', 'dialog');
          dialog.innerHTML = '<h2>Invite Jane Prospect</h2><button>Send</button>';
          document.body.append(dialog);
        }""")
        self.assertEqual(
            assert_profile_identity(self.page, identity).identity, identity
        )

    def test_hidden_topcard_is_not_verified(self):
        self.load_html(
            profile_html(
                topcard=TOPCARD.replace(
                    "<section ", '<section style="display:none" ', 1
                )
            )
        )
        self.assert_skipped("profile_not_ready")


class ProfileReadinessBrowserTests(OfflineBrowserTestCase):
    def test_five_second_delayed_about_is_not_preempted_by_stable_page_size(self):
        script = f"""<script>setTimeout(() => {{
          const section = document.createElement('section'); section.id = 'about';
          section.innerHTML = '<h2>About</h2><p>{ABOUT}</p>';
          document.querySelector('main').append(section);
        }}, 5000);</script>"""
        self.load_html(profile_html(sections="") + script)
        started = time.monotonic()
        snapshot = wait_for_profile(self.page, URL, timeout_ms=7500)
        self.assertGreaterEqual(time.monotonic() - started, 4.8)
        self.assertIn(ABOUT, snapshot.content)

    def test_experience_arriving_after_about_is_required(self):
        sections = (
            f'<section id="about"><h2>About</h2><p>{ABOUT}</p></section>'
            + '<section id="experience"><h2>Experience</h2></section>'
        )
        self.load_html(
            profile_html(sections=sections)
            + f"""<script>setTimeout(() => {{
                         document.getElementById('experience').innerHTML = '<h2>Experience</h2><p>{EXPERIENCE}</p>';
                       }}, 700);</script>"""
        )
        snapshot = wait_for_profile(self.page, URL, timeout_ms=2200)
        self.assertIn(EXPERIENCE, snapshot.content)

    def test_same_length_replacements_reset_content_stability(self):
        old = "A" * 220
        new = "B" * 220
        self.load_html(
            profile_html(
                sections=f'<section><h2>About</h2><p id="changing">{old}</p></section>'
                + f"<section><h2>Experience</h2><p>{EXPERIENCE}</p></section>"
            )
            + f"<script>setTimeout(() => document.getElementById('changing').textContent = '{new}', 350);</script>"
        )
        started = time.monotonic()
        snapshot = wait_for_profile(self.page, URL, timeout_ms=1800)
        self.assertGreaterEqual(time.monotonic() - started, 0.85)
        self.assertIn(new, snapshot.content)
        self.assertNotIn(old, snapshot.content)

    def test_busy_sections_wait_for_real_loaded_content(self):
        sections = SECTIONS.replace('id="about"', 'id="about" aria-busy="true"')
        self.load_html(
            profile_html(sections=sections)
            + "<script>setTimeout(() => document.getElementById('about').removeAttribute('aria-busy'), 500);</script>"
        )
        started = time.monotonic()
        snapshot = wait_for_profile(self.page, URL, timeout_ms=2000)
        self.assertGreaterEqual(time.monotonic() - started, 0.95)
        self.assertIn(ABOUT, snapshot.content)

    def test_skeleton_sections_are_never_personalized(self):
        self.load_html(
            profile_html(
                sections=SECTIONS.replace('id="about"', 'id="about" class="skeleton"')
            )
        )
        snapshot = wait_for_profile(self.page, URL, timeout_ms=250)
        self.assertEqual(snapshot.content, "")
        self.assertEqual(snapshot.identity.name, "Jane Prospect")

    def test_missing_empty_or_thin_sections_fall_back_to_no_note(self):
        for sections in (
            "",
            "<section><h2>About</h2></section><section><h2>Experience</h2></section>",
            "<section><h2>About</h2><p>Hi</p></section><section><h2>Experience</h2><p>Engineer</p></section>",
            f"<section><h2>About</h2><p>{ABOUT}</p></section><section><h2>Experience</h2><p>Loading…</p></section>",
        ):
            with self.subTest(sections=sections):
                self.load_html(profile_html(sections=sections))
                snapshot = wait_for_profile(self.page, URL, timeout_ms=200)
                self.assertEqual(snapshot.content, "")
                self.assertEqual(snapshot.identity.name, "Jane Prospect")

    def test_about_only_and_experience_only_can_personalize(self):
        for title, value in (("About", ABOUT), ("Experience", EXPERIENCE)):
            with self.subTest(title=title):
                self.load_html(
                    profile_html(
                        sections=f"<section><h2>{title}</h2><p>{value}</p></section>"
                    )
                )
                snapshot = wait_for_profile(self.page, URL, timeout_ms=1600)
                self.assertIn(value, snapshot.content)
                self.assertEqual(snapshot.identity.name, "Jane Prospect")

    def test_identity_only_does_not_wait_for_content(self):
        self.load_html(profile_html(sections=""))
        started = time.monotonic()
        snapshot = wait_for_profile(self.page, URL, personalize=False, timeout_ms=3000)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(snapshot.content, "")

    def test_identity_drift_during_hydration_fails_closed(self):
        self.load_html(
            profile_html(sections="")
            + """<script>setTimeout(() => {
          document.querySelector('.pv-top-card a').href = '/in/other/';
        }, 200);</script>"""
        )
        with self.assertRaises(TaskSkippedException) as raised:
            wait_for_profile(self.page, URL, timeout_ms=1200)
        self.assertEqual(raised.exception.reason, "profile_identity_mismatch")

    def test_busy_topcard_has_no_established_identity(self):
        self.load_html(
            profile_html(
                topcard=TOPCARD.replace("<section ", '<section aria-busy="true" ', 1)
            )
        )
        with self.assertRaises(TaskSkippedException) as raised:
            wait_for_profile(self.page, URL, personalize=False, timeout_ms=200)
        self.assertEqual(raised.exception.reason, "profile_not_ready")


if __name__ == "__main__":
    unittest.main()
