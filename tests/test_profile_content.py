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
    is_profile_owned_element,
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
SDUI_EXPERIENCE = """<div componentkey="com.linkedin.sdui.profile.card.refTARGETExperienceTopLevelSection">
  <div data-display-contents="true"><section>
    <div>
      <div data-display-contents="true">
        <div componentkey="Profile_Top_Level_ExperienceTopLevelSectionjane-prospect"></div>
      </div>
      <div data-display-contents="true"><div>
      <div><h2 componentkey="ProfileNullStateCardAnchor_Experience">Experience</h2></div>
      <div data-component-type="LazyColumn">
        <div componentkey="entity-collection-item-first"><div>
          <a href="https://www.linkedin.com/company/orchard/"><div>
            <div><p>Staff Engineer</p><p>Orchard Analytics · Full-time</p></div>
            <p>Jun 2011 - Present · 15 yrs 5 mos</p>
            <p>Bengaluru, Karnataka, India</p>
          </div></a>
          <div><p><span>Leading reliable inventory ingestion and analytics platforms.</span></p></div>
        </div></div>
        <div componentkey="entity-collection-item-second"><div>
          <a href="https://www.linkedin.com/company/previous/"><div>
            <div><p>Senior Engineer</p><p>Previous Systems · Full-time</p></div>
            <p>Jan 2007 - May 2011 · 4 yrs 5 mos</p>
          </div></a>
        </div></div>
      </div>
      </div></div>
    </div>
  </section></div>
</div>"""


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


class SduiProfileBrowserTests(OfflineBrowserTestCase):
    def test_nested_layout_preserves_identity_owned_content_and_actions(self):
        self.load_html(sdui_profile_html())
        snapshot = wait_for_profile(self.page, URL, timeout_ms=1200)
        self.assertEqual(
            snapshot.identity, ProfileIdentity(URL, "jane-prospect", "Jane Prospect")
        )
        self.assertEqual(snapshot.about, ABOUT)
        self.assertEqual(snapshot.headline, "Building data platforms at Orchard Analytics")
        self.assertNotIn("shared post", snapshot.content)
        self.assertNotIn("Mutual Person", snapshot.content)
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
        self.assertIn("5,000 followers", snapshot.audience_text)
        self.assertIn("500+", snapshot.audience_text)

    def test_observed_experience_top_level_section_supplies_personalization(self):
        short_about = (
            "I build reliable platforms with experienced data engineering teams "
            "across industries."
        )
        self.load_html(sdui_profile_html(SDUI_EXPERIENCE).replace(ABOUT, short_about))
        snapshot = wait_for_profile(self.page, URL, timeout_ms=1200)
        self.assertEqual(snapshot.about, short_about)
        self.assertEqual(snapshot.headline, "Building data platforms at Orchard Analytics")
        self.assertIn("Staff Engineer", snapshot.experience)
        self.assertIn("Orchard Analytics · Full-time", snapshot.experience)
        self.assertIn("Previous Systems · Full-time", snapshot.experience)
        self.assertIn("Experience:", snapshot.content)
        self.assertNotIn("Example University", snapshot.content)
        self.assertNotIn("shared post", snapshot.content)
        self.assertNotIn("Mutual Person", snapshot.content)

    def test_both_exact_owned_experience_suffixes_remain_supported(self):
        for suffix in ("Experience", "ExperienceTopLevelSection"):
            with self.subTest(suffix=suffix):
                self.load_html(
                    sdui_profile_html(
                        SDUI_EXPERIENCE.replace("refTARGETExperienceTopLevelSection", "refTARGET" + suffix)
                    )
                )
                snapshot = read_profile_snapshot(self.page, URL)
                self.assertIn("Staff Engineer", snapshot.experience)
                self.assertTrue(snapshot.content)

    def test_foreign_owner_and_near_match_experience_keys_are_not_owned(self):
        for key in (
            "refOTHERExperienceTopLevelSection",
            "refTARGET-extraExperienceTopLevelSection",
            "refTARGETExperienceTopLevelSection-extra",
        ):
            with self.subTest(key=key):
                self.load_html(
                    sdui_profile_html(
                        SDUI_EXPERIENCE.replace("refTARGETExperienceTopLevelSection", key)
                    )
                )
                snapshot = read_profile_snapshot(self.page, URL)
                self.assertEqual(snapshot.experience, "")
                self.assertEqual(snapshot.content, "")

    def test_experience_in_different_lazy_column_is_not_owned(self):
        experience = f'<div data-component-type="LazyColumn">{SDUI_EXPERIENCE}</div>'
        self.load_html(sdui_profile_html(experience))
        snapshot = read_profile_snapshot(self.page, URL)
        self.assertEqual(snapshot.experience, "")
        self.assertEqual(snapshot.content, "")

    def test_boxless_experience_wrappers_preserve_visibility_and_exclusions(self):
        extra = """
          <div data-display-contents="true">
            <span data-display-contents="true">Visible boxless role description</span>
            <p style="display:none">Hidden role details</p>
            <div style="display:none"><span data-display-contents="true">Hidden direct text</span></div>
            <span data-display-contents="true" style="visibility:hidden">Invisible direct text</span>
            <div style="visibility:hidden"><span data-display-contents="true">Invisible ancestor text</span></div>
            <button>Button marketing prose</button>
            <div role="button">Action marketing prose</div>
            <aside><p>Sidebar marketing prose</p></aside>
            <section><h3>Recommended person</h3>
              <a href="/in/foreign/">Foreign Person</a><p>Foreign marketing prose</p>
            </section>
          </div>
        """
        self.load_html(
            sdui_profile_html(SDUI_EXPERIENCE.replace("</section>", extra + "</section>"))
        )
        snapshot = read_profile_snapshot(self.page, URL)
        self.assertIn("Staff Engineer", snapshot.experience)
        self.assertIn("Visible boxless role description", snapshot.experience)
        self.assertNotIn("Hidden", snapshot.experience)
        self.assertNotIn("Invisible", snapshot.experience)
        self.assertNotIn("marketing", snapshot.experience)
        self.assertNotIn("Foreign", snapshot.experience)

    def test_boxless_loading_wrapper_requires_rendered_owned_content(self):
        for hidden_style in ("", "display:none", "visibility:hidden"):
            with self.subTest(hidden_style=hidden_style):
                loading = (
                    f'<div style="{hidden_style}"><div data-display-contents="true" aria-busy="true">'
                    "<p>Loading role details</p></div></div>"
                )
                self.load_html(
                    sdui_profile_html(SDUI_EXPERIENCE.replace("</section>", loading + "</section>"))
                )
                snapshot = read_profile_snapshot(self.page, URL)
                if hidden_style:
                    self.assertIn("Staff Engineer", snapshot.content)
                    self.assertNotIn("Loading role details", snapshot.experience)
                else:
                    self.assertEqual(snapshot.experience, "")
                    self.assertEqual(snapshot.content, "")

    def test_experience_does_not_borrow_recommendation_prose(self):
        experience = SDUI_EXPERIENCE.replace(
            "</section>",
            '<section><h3>People you may know</h3><a href="/in/foreign/">'
            "Foreign Person</a><p>Foreign aerospace executive biography</p></section></section>",
        )
        self.load_html(sdui_profile_html(experience))
        snapshot = read_profile_snapshot(self.page, URL)
        self.assertIn("Staff Engineer", snapshot.content)
        self.assertNotIn("Foreign", snapshot.content)
        self.assertNotIn("aerospace", snapshot.experience)

    def test_duplicate_foreign_experience_card_cannot_contaminate_owned_text(self):
        foreign = SDUI_EXPERIENCE.replace("refTARGET", "refOTHER").replace(
            "Staff Engineer", "Foreign Executive"
        )
        self.load_html(sdui_profile_html(SDUI_EXPERIENCE + foreign))
        snapshot = read_profile_snapshot(self.page, URL)
        self.assertEqual(snapshot.experience, "")
        self.assertEqual(snapshot.content, "")

    def test_headline_does_not_scan_adjacent_or_recommendation_text(self):
        for replacement in (
            "<div><p>Adjacent aerospace biography</p></div>",
            '<section><h3>Recommended person</h3><p>Adjacent aerospace biography</p></section>',
            '<p><a href="/in/foreign/">Adjacent aerospace biography</a></p>',
        ):
            with self.subTest(replacement=replacement):
                html = sdui_profile_html().replace(
                    "<p>Building data platforms at Orchard Analytics</p>",
                    replacement,
                )
                self.load_html(html)
                snapshot = read_profile_snapshot(self.page, URL)
                self.assertEqual(snapshot.headline, "")
                self.assertNotIn("aerospace", snapshot.content)

    def test_headline_requires_a_bounded_name_and_degree_row(self):
        html = sdui_profile_html().replace(
            "<p>· 2nd</p>", "<p>Someone else's unbounded biography</p>"
        )
        self.load_html(html)
        snapshot = read_profile_snapshot(self.page, URL)
        self.assertEqual(snapshot.headline, "")

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

    def test_foreign_keyed_about_is_not_owned(self):
        self.load_html(
            sdui_profile_html().replace("card.refTARGETAbout", "card.refOTHERAbout")
        )
        snapshot = wait_for_profile(self.page, URL, timeout_ms=200)
        self.assertEqual(snapshot.identity.name, "Jane Prospect")
        self.assertEqual(snapshot.about, "")
        self.assertEqual(snapshot.content, "")

    def test_topcard_inside_recommendation_module_is_not_primary(self):
        self.load_html(
            sdui_profile_html().replace(
                "<main><section>", "<main><section><h2>People you may know</h2>"
            )
        )
        with self.assertRaises(TaskSkippedException) as raised:
            wait_for_profile(self.page, URL, timeout_ms=200)
        self.assertEqual(raised.exception.reason, "profile_not_ready")

    def test_about_inside_foreign_module_is_not_owned(self):
        html = sdui_profile_html().replace(
            '<div componentkey="profileCardsAboveActivity">',
            '<div componentkey="profileCardsAboveActivity"><h2>Other Person</h2>',
        )
        self.load_html(html)
        snapshot = wait_for_profile(self.page, URL, timeout_ms=200)
        self.assertEqual(snapshot.about, "")
        self.assertEqual(snapshot.content, "")


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

    def test_missing_empty_or_thin_sections_leave_content_unavailable(self):
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
