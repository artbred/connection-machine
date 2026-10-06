"""Exercise owned DOM extraction against adversarial profile module layouts."""

import unittest

from tests.browser_case import OfflineBrowserTestCase
from tests.test_profile_content import (
    ABOUT,
    EXPERIENCE,
    SECTIONS,
    TOPCARD,
    URL,
    profile_html,
)
from linkedin_profile import is_profile_owned_element, read_profile_snapshot


class OwnedExtractionBrowserTests(OfflineBrowserTestCase):
    def snapshot(self):
        snapshot = read_profile_snapshot(self.page, URL)
        self.assertIsNotNone(snapshot)
        return snapshot

    def test_only_named_owner_fields_enter_personalization(self):
        self.load_html(
            profile_html(
                extra="""
          <div><div role="heading" aria-level="2">More profiles for you</div>
            <div>Alex Stranger — Aerospace Engineering Graduate; led satellite propulsion research.</div></div>
          <div><div role="heading" aria-level="2">Activity</div>
            <div>Jane Prospect commented on this</div>
            <div>Blair Other — Sales coaching director; enterprise cold outreach training.</div></div>
          <section><h2>Education</h2><div>Unrequested university facts</div></section>
          <section><h2>Unknown module</h2><div>Unowned mystery content</div></section>
          <aside>Sidebar advert</aside><footer>LinkedIn Corporation</footer>
          <div role="dialog">Invitation note text</div>
        """
            )
        )
        snapshot = self.snapshot()
        self.assertEqual(snapshot.identity.name, "Jane Prospect")
        self.assertEqual(snapshot.about, ABOUT)
        self.assertEqual(snapshot.experience, EXPERIENCE)
        self.assertIn("Building data platforms", snapshot.content)
        for foreign in (
            "Alex Stranger",
            "Aerospace",
            "satellite",
            "Blair Other",
            "Sales coaching",
            "Activity",
            "Education",
            "university",
            "mystery",
            "Sidebar",
            "Corporation",
            "Invitation",
            "followers",
            "Connect",
        ):
            with self.subTest(foreign=foreign):
                self.assertNotIn(foreign, snapshot.content)
        self.assertIn("5,000 followers", snapshot.audience_text)

    def test_nested_foreign_profile_cards_are_removed_with_their_prose(self):
        sections = f"""<section><h2>About</h2><p>{ABOUT}</p>
          <div class="recommendation"><a href="/in/alex-stranger/">Alex Stranger</a>
            <div>Satellite propulsion research and orbital navigation.</div><button>Connect</button></div>
        </section><section><h2>Experience</h2><p>{EXPERIENCE}</p>
          <ul><li><a href="/in/blair-other/">Blair Other</a><p>Cold outreach training business.</p></li></ul>
        </section>"""
        self.load_html(profile_html(sections=sections))
        snapshot = self.snapshot()
        self.assertIn(ABOUT, snapshot.content)
        self.assertIn(EXPERIENCE, snapshot.content)
        for foreign in ("Alex Stranger", "Satellite", "Blair Other", "Cold outreach"):
            self.assertNotIn(foreign, snapshot.content)

    def test_unknown_nested_heading_cannot_leak_unlinked_foreign_text(self):
        sections = SECTIONS.replace(
            f"<p>{ABOUT}</p>",
            f"""<p>{ABOUT}</p>
          <div><div role="heading" aria-level="3">Someone else's story</div>
          <p>Unlinked stranger invented orbital navigation systems.</p></div>""",
        )
        self.load_html(profile_html(sections=sections))
        snapshot = self.snapshot()
        self.assertIn(ABOUT, snapshot.content)
        self.assertNotIn("orbital", snapshot.content)
        self.assertNotIn("Someone else", snapshot.content)

    def test_activity_nested_inside_owned_section_is_not_owned_prose(self):
        sections = SECTIONS.replace(
            f"<p>{ABOUT}</p>",
            f"""<p>{ABOUT}</p>
          <div><h3>Activity</h3><div>Jane Prospect likes this</div>
          <div>Foreign post about cryptocurrency trading signals.</div></div>""",
        )
        self.load_html(profile_html(sections=sections))
        self.assertNotIn("cryptocurrency", self.snapshot().content)

    def test_ambiguous_shared_heading_root_drops_entire_section(self):
        sections = (
            f"<section><h2>About</h2><p>{ABOUT}</p><h2>Activity</h2><p>Foreign facts</p></section>"
            + f"<section><h2>Experience</h2><p>{EXPERIENCE}</p></section>"
        )
        self.load_html(profile_html(sections=sections))
        snapshot = self.snapshot()
        self.assertEqual(snapshot.about, "")
        self.assertEqual(snapshot.content, "")

    def test_about_inside_recommendation_module_is_not_owner_about(self):
        sections = f"""<div><h2>More profiles for you</h2>
          <section><h3>About</h3><p>{ABOUT}</p></section></div>
          <section><h2>Experience</h2><p>{EXPERIENCE}</p></section>"""
        self.load_html(profile_html(sections=sections))
        snapshot = self.snapshot()
        self.assertEqual(snapshot.about, "")
        self.assertEqual(snapshot.content, "")

    def test_div_role_heading_sections_are_bounded(self):
        sections = f"""<div><div role="heading" aria-level="2">About</div><p>{ABOUT}</p></div>
          <div><div role="heading" aria-level="2">Experience</div><p>{EXPERIENCE}</p></div>
          <div><div role="heading" aria-level="2">Activity</div><p>Unowned activity facts</p></div>"""
        self.load_html(profile_html(sections=sections))
        snapshot = self.snapshot()
        self.assertEqual(snapshot.about, ABOUT)
        self.assertEqual(snapshot.experience, EXPERIENCE)
        self.assertNotIn("Unowned activity", snapshot.content)

    def test_h3_sections_are_supported(self):
        self.load_html(profile_html(sections=SECTIONS.replace("h2", "h3")))
        snapshot = self.snapshot()
        self.assertIn(ABOUT, snapshot.content)
        self.assertIn(EXPERIENCE, snapshot.content)

    def test_employer_linked_experience_job_heading_is_owned(self):
        sections = f"""<section><h2>Experience</h2><ul><li>
          <h3>Staff Data Engineer</h3>
          <a href="https://www.linkedin.com/company/orchard/">Orchard Analytics</a>
          <p>{EXPERIENCE}</p></li></ul></section>"""
        self.load_html(profile_html(sections=sections))
        snapshot = self.snapshot()
        self.assertIn("Staff Data Engineer", snapshot.experience)
        self.assertIn("Orchard Analytics", snapshot.content)
        self.assertIn(EXPERIENCE, snapshot.content)

    def test_employer_link_does_not_authorize_another_person_experience(self):
        sections = f"""<section><h2>Experience</h2><ul><li>
          <h3>Staff Data Engineer</h3>
          <a href="https://www.linkedin.com/company/orchard/">Orchard Analytics</a>
          <p>{EXPERIENCE}</p></li><li>
          <h3>Another person's job</h3><a href="/in/stranger/">Alex Stranger</a>
          <a href="https://www.linkedin.com/company/moonshot/">Moonshot Labs</a>
          <p>Foreign satellite propulsion achievements</p>
          </li></ul></section>"""
        self.load_html(profile_html(sections=sections))
        snapshot = self.snapshot()
        self.assertIn(EXPERIENCE, snapshot.content)
        self.assertNotIn("Moonshot", snapshot.content)
        self.assertNotIn("satellite propulsion", snapshot.content)

    def test_legacy_name_h2_and_contact_marker(self):
        topcard = f'''<section><h2>Jane Prospect</h2>
          <div>Building data platforms at Orchard Analytics</div>
          <a href="{URL}overlay/contact-info/">Contact info</a><button>Connect</button></section>'''
        self.load_html(profile_html(topcard=topcard))
        snapshot = self.snapshot()
        self.assertEqual(
            snapshot.headline, "Building data platforms at Orchard Analytics"
        )
        self.assertIn(ABOUT, snapshot.content)

    def test_sdui_topcard_role_heading_and_target_action_marker(self):
        topcard = f'''<div data-view-name="profile-top-card">
          <div role="heading" aria-level="1">Jane Prospect</div>
          <div data-view-name="profile-headline">Building data platforms at Orchard Analytics</div>
          <a href="{URL}overlay/connect/" aria-label="Invite Jane Prospect to connect">Connect</a></div>'''
        self.load_html(profile_html(topcard=topcard))
        self.assertIn(ABOUT, self.snapshot().content)

    def test_wrapped_primary_column_excludes_other_columns(self):
        self.load_html(f"""<main><div id="primary">{TOPCARD}{SECTIONS}</div>
          <aside><section><h2>About</h2><p>Stranger biography.</p></section></aside></main>""")
        self.assertEqual(self.snapshot().about, ABOUT)

    def test_nested_foreign_card_does_not_claim_topcard_identity_or_stats(self):
        nested = """<section><h2><a href="/in/foreign-person/">Foreign Person</a></h2>
          <div>Space propulsion</div><div>900,000 followers</div><button id="foreign">Connect</button></section>"""
        self.load_html(
            profile_html(topcard=TOPCARD.replace("</section>", nested + "</section>"))
        )
        snapshot = self.snapshot()
        self.assertEqual(snapshot.identity.name, "Jane Prospect")
        self.assertNotIn("900,000", snapshot.audience_text)
        self.assertTrue(
            is_profile_owned_element(
                self.page.locator("#connect"), self.page, snapshot.identity
            )
        )
        self.assertFalse(
            is_profile_owned_element(
                self.page.locator("#foreign"), self.page, snapshot.identity
            )
        )

    def test_generic_nested_div_foreign_action_is_not_owner_action(self):
        nested = """<div><a href="/in/foreign-person/">Foreign Person</a>
          <div>Space propulsion</div><button id="foreign">Connect</button></div>"""
        self.load_html(
            profile_html(topcard=TOPCARD.replace("</section>", nested + "</section>"))
        )
        snapshot = self.snapshot()
        self.assertTrue(
            is_profile_owned_element(
                self.page.locator("#connect"), self.page, snapshot.identity
            )
        )
        self.assertFalse(
            is_profile_owned_element(
                self.page.locator("#foreign"), self.page, snapshot.identity
            )
        )

    def test_legacy_aria_hidden_display_text_survives_without_duplicates(self):
        sections = SECTIONS.replace(
            "<h2>About</h2>",
            '<h2><span aria-hidden="true">About</span><span class="visually-hidden">About</span></h2>',
        ).replace(
            f"<p>{ABOUT}</p>",
            f'<p><span aria-hidden="true">{ABOUT}</span><span class="visually-hidden">{ABOUT}</span></p>',
        )
        self.load_html(profile_html(sections=sections))
        snapshot = self.snapshot()
        self.assertEqual(snapshot.about, ABOUT)
        self.assertEqual(snapshot.content.count(ABOUT), 1)

    def test_foreign_module_loading_does_not_block_owner_readiness(self):
        nested = """<div><h3>More profiles for you</h3><div class="skeleton" aria-busy="true">Loading</div></div>"""
        self.load_html(
            profile_html(
                topcard=TOPCARD.replace("</section>", nested + "</section>"),
                extra=nested,
            )
        )
        self.assertIn(ABOUT, self.snapshot().content)

    def test_read_only_extraction_preserves_attributes_dom_and_scroll(self):
        self.load_html(
            profile_html(
                extra='<div style="height:1800px">Unknown page remainder</div>'
            )
        )
        self.page.evaluate("() => window.scrollTo(0, 160)")
        before = self.page.evaluate(
            "() => ({html: document.documentElement.outerHTML, x: scrollX, y: scrollY})"
        )
        for _ in range(3):
            self.snapshot()
        after = self.page.evaluate(
            "() => ({html: document.documentElement.outerHTML, x: scrollX, y: scrollY})"
        )
        self.assertEqual(after, before)

    def test_fixture_requests_cannot_escape_to_network(self):
        self.load_html(
            profile_html(extra='<img src="https://example.invalid/foreign-image.png">')
        )
        self.page.wait_for_load_state("load")
        self.assertIn(
            "https://example.invalid/foreign-image.png", self.blocked_requests
        )
        self.assertEqual(self.snapshot().identity.name, "Jane Prospect")


if __name__ == "__main__":
    unittest.main()
