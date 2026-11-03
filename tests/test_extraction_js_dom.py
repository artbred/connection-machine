"""Exercise owner identity and action ownership against adversarial profile layouts."""

import unittest

from tests.browser_case import OfflineBrowserTestCase
from tests.test_profile_content import TOPCARD, URL, profile_html
from linkedin_profile import is_profile_owned_element, read_profile_snapshot


class OwnedExtractionBrowserTests(OfflineBrowserTestCase):
    def snapshot(self):
        snapshot = read_profile_snapshot(self.page, URL)
        self.assertIsNotNone(snapshot)
        return snapshot

    def test_legacy_name_h2_and_contact_marker(self):
        topcard = f'''<section><h2>Jane Prospect</h2>
          <div>Building data platforms at Orchard Analytics</div>
          <a href="{URL}overlay/contact-info/">Contact info</a><button>Connect</button></section>'''
        self.load_html(profile_html(topcard=topcard))
        self.assertEqual(self.snapshot().identity.name, "Jane Prospect")

    def test_sdui_topcard_role_heading_and_target_action_marker(self):
        topcard = f'''<div data-view-name="profile-top-card">
          <div role="heading" aria-level="1">Jane Prospect</div>
          <div data-view-name="profile-headline">Building data platforms at Orchard Analytics</div>
          <a href="{URL}overlay/connect/" aria-label="Invite Jane Prospect to connect">Connect</a></div>'''
        self.load_html(profile_html(topcard=topcard))
        self.assertEqual(self.snapshot().identity.name, "Jane Prospect")

    def test_nested_foreign_card_does_not_claim_topcard_identity(self):
        nested = """<section><h2><a href="/in/foreign-person/">Foreign Person</a></h2>
          <div>Space propulsion</div><div>900,000 followers</div><button id="foreign">Connect</button></section>"""
        self.load_html(
            profile_html(topcard=TOPCARD.replace("</section>", nested + "</section>"))
        )
        snapshot = self.snapshot()
        self.assertEqual(snapshot.identity.name, "Jane Prospect")
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

    def test_foreign_module_loading_does_not_block_owner_identity(self):
        nested = """<div><h3>More profiles for you</h3><div class="skeleton" aria-busy="true">Loading</div></div>"""
        self.load_html(
            profile_html(
                topcard=TOPCARD.replace("</section>", nested + "</section>"),
                extra=nested,
            )
        )
        self.assertEqual(self.snapshot().identity.name, "Jane Prospect")

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
