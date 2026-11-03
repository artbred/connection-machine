"""Canonical identity and bounded profile hydration behavior, without live access."""

import unittest
from dataclasses import FrozenInstanceError

from tests.browser_case import OfflineBrowserTestCase
from exceptions import TaskSkippedException
from linkedin_profile import (
    ProfileDetails,
    ProfileIdentity,
    assert_profile_identity,
    canonical_profile_url,
    get_profile_topcard,
    is_profile_owned_element,
    read_profile_snapshot,
    verify_profile_details,
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


def sdui_profile_html(experience=""):
    # Production SDUI uses a layout section around a LazyColumn, then keyed
    # cards inside wrapper groups; the nearest section is not the whole column.
    prefix = "com.linkedin.sdui.profile.card.refTARGET"
    return f"""<meta charset="utf-8"><style>[data-display-contents="true"] {{ display: contents; }}</style>
    <main><section><div data-component-type="LazyColumn">
      <div componentkey="{prefix}Topcard"><div data-display-contents="true"><section id="target-card">
        <div><div><div data-display-contents="true">
          <a componentkey="ProfileVerificationTriggerRef-jane-prospect" href="{URL}">
            <div><div><h2>Jane Prospect</h2><svg aria-label="View verifications"></svg></div></div>
          </a>
        </div></div>
          <div data-display-contents="true" style="display:none"><p>· 1st</p></div>
          <p>· 2nd</p>
        </div>
        <p>Building data platforms at Orchard Analytics</p>
        <p>Orchard Analytics · Example University</p>
        <div><a href="{URL}overlay/contact-info/">Contact info</a></div>
        <p>5,000 followers</p><div><p>500+</p><p>connections</p></div>
        <button id="target-connect">Connect</button>
      </section></div></div>
      <div componentkey="profileCardsAboveActivity">
        <div componentkey="{prefix}Highlights"><section><h2>Highlights</h2>
          <a href="/in/mutual-person/">Mutual Person</a></section></div>
        <div componentkey="{prefix}About"><div><section><h2>About</h2>
          <p>{ABOUT}</p></section></div></div>
      </div>
      {experience}
      <div componentkey="{prefix}Activity"><section><h2>Activity</h2>
        <p>Unrelated shared post content must not personalize an invitation.</p>
        <a href="/in/foreign/">Foreign Person</a><button id="foreign-connect">Connect</button>
      </section></div>
    </div></section></main>"""


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
        snapshot = wait_for_profile(self.page, URL)
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
        identity = wait_for_profile(self.page, URL).identity
        self.page.locator("h1").evaluate("el => el.textContent = 'Other Person'")
        with self.assertRaises(TaskSkippedException) as raised:
            assert_profile_identity(self.page, identity)
        self.assertEqual(raised.exception.reason, "profile_identity_mismatch")

    def test_invitation_overlay_does_not_erase_underlying_identity(self):
        self.load_html(profile_html())
        identity = wait_for_profile(self.page, URL).identity
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


class SduiProfileBrowserTests(OfflineBrowserTestCase):
    def test_nested_layout_preserves_identity_and_owned_actions(self):
        self.load_html(sdui_profile_html())
        snapshot = wait_for_profile(self.page, URL, timeout_ms=1200)
        self.assertEqual(
            snapshot.identity, ProfileIdentity(URL, "jane-prospect", "Jane Prospect")
        )
        self.assertEqual(
            get_profile_topcard(self.page, snapshot.identity).get_attribute("id"),
            "target-card",
        )
        self.assertTrue(
            is_profile_owned_element(
                self.page.locator("#target-connect"), self.page, snapshot.identity
            )
        )
        self.assertFalse(
            is_profile_owned_element(
                self.page.locator("#foreign-connect"), self.page, snapshot.identity
            )
        )

    def test_card_marker_alone_does_not_prove_recipient(self):
        self.load_html(sdui_profile_html().replace(f'href="{URL}', 'href="/unknown/'))
        with self.assertRaises(TaskSkippedException) as raised:
            wait_for_profile(self.page, URL, timeout_ms=200)
        self.assertEqual(raised.exception.reason, "profile_not_ready")

    def test_stale_sdui_identity_is_rejected(self):
        self.load_html(
            sdui_profile_html().replace(URL, "https://www.linkedin.com/in/old-person/")
        )
        with self.assertRaises(TaskSkippedException) as raised:
            wait_for_profile(self.page, URL, timeout_ms=200)
        self.assertEqual(raised.exception.reason, "profile_identity_mismatch")

    def test_topcard_inside_recommendation_module_is_not_primary(self):
        self.load_html(
            sdui_profile_html().replace(
                "<main><section>", "<main><section><h2>People you may know</h2>"
            )
        )
        with self.assertRaises(TaskSkippedException) as raised:
            wait_for_profile(self.page, URL, timeout_ms=200)
        self.assertEqual(raised.exception.reason, "profile_not_ready")

    def test_topcard_in_unkeyed_column_wrapper_is_verified(self):
        # Observed 2026-11-03: LinkedIn wraps each LazyColumn item in a plain div.
        self.load_html(sdui_profile_html())
        self.page.evaluate("""() => {
          const card = document.querySelector('[componentkey$="Topcard"]');
          const wrapper = document.createElement('div');
          card.replaceWith(wrapper);
          wrapper.append(card);
        }""")
        snapshot = wait_for_profile(self.page, URL, timeout_ms=1200)
        self.assertEqual(snapshot.identity.name, "Jane Prospect")
        self.assertTrue(
            is_profile_owned_element(
                self.page.locator("#target-connect"), self.page, snapshot.identity
            )
        )

    def test_wrapper_shared_with_another_module_does_not_join_the_column(self):
        self.load_html(sdui_profile_html())
        self.page.evaluate("""() => {
          const card = document.querySelector('[componentkey$="Topcard"]');
          const wrapper = document.createElement('div');
          card.replaceWith(wrapper);
          wrapper.append(card, Object.assign(document.createElement('p'), {textContent: 'Promoted'}));
        }""")
        with self.assertRaises(TaskSkippedException) as raised:
            wait_for_profile(self.page, URL, timeout_ms=200)
        self.assertEqual(raised.exception.reason, "profile_not_ready")


class ProfileReadinessBrowserTests(OfflineBrowserTestCase):
    def test_busy_topcard_has_no_established_identity(self):
        self.load_html(
            profile_html(
                topcard=TOPCARD.replace("<section ", '<section aria-busy="true" ', 1)
            )
        )
        with self.assertRaises(TaskSkippedException) as raised:
            wait_for_profile(self.page, URL, timeout_ms=200)
        self.assertEqual(raised.exception.reason, "profile_not_ready")


class ProfileDetailsVerificationTests(unittest.TestCase):
    PAGE = (
        "Jane Prospect\n· 2nd\nBuilding data platforms\nActivity\n2,312 followers\n"
        "500+\nconnections\nAbout\n" + ABOUT + "\nExperience\nStaff Engineer\n\n"
        "Orchard  Analytics · Full-time\nMore profiles for you\nAlex Stranger"
    )

    def test_keeps_only_fields_found_verbatim_on_the_page(self):
        details = verify_profile_details(self.PAGE, {
            "headline": "Building data platforms",
            "about": ABOUT,
            "experience": "Staff Engineer\nOrchard Analytics · Full-time",
            "followers_text": "2,312 followers",
            "connections_text": "500+ connections",
        })
        self.assertEqual(details.experience, "Staff Engineer\nOrchard Analytics · Full-time")
        self.assertEqual(details.connections_text, "500+ connections")
        self.assertEqual(details.about, ABOUT)

    def test_invented_or_malformed_values_are_dropped(self):
        details = verify_profile_details(self.PAGE, {
            "headline": "Building data platforms at NASA",
            "about": ABOUT + "\nFormer astronaut",
            "experience": None,
            "followers_text": "20,000 followers",
            "connections_text": "None",
        })
        self.assertEqual(details, ProfileDetails())
        self.assertIsNone(verify_profile_details(self.PAGE, None))

    def test_content_requires_enough_owner_prose(self):
        self.assertEqual(ProfileDetails(headline="Engineer", about="Hi").content("Jane"), "")
        content = ProfileDetails(headline="Engineer", about=ABOUT).content("Jane Prospect")
        self.assertTrue(content.startswith("Jane Prospect\n\nHeadline:\nEngineer"))
        self.assertIn("About:\n" + ABOUT, content)


if __name__ == "__main__":
    unittest.main()
