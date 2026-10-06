import os
import unittest
from unittest.mock import patch

from patchright.sync_api import Locator as BrowserLocator
from patchright.sync_api import TimeoutError as BrowserTimeoutError

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from tests.browser_case import OfflineBrowserTestCase
from connect_heuristics import (
    get_cached_connect_button,
    is_target_action,
    save_selector_to_cache,
    selector_cache,
    try_heuristic_connect,
)
from connection_state import ConnectionState, detect_connection_state
from exceptions import TaskSkippedException
from human_actions import HumanActions
from linkedin_profile import ProfileIdentity, wait_for_profile
from tasks.notification_reply_invites import NotificationReplyInviteScanner


URL = "https://www.linkedin.com/in/jane-prospect/"
IDENTITY = ProfileIdentity(URL, "jane-prospect", "Jane Prospect")


def profile_html(actions="", *, before="", after="", inside=""):
    return f"""<!doctype html><html><body>{before}<main>
        <section class="pv-top-card" id="topcard">
          <a href="/in/jane-prospect/"><h1>Jane Prospect</h1></a>
          <div class="text-body-medium">Target-owned headline</div>
          <div class="pvs-profile-actions">{actions}</div>{inside}
        </section>{after}</main></body></html>"""


class TargetActionBrowserTests(OfflineBrowserTestCase):
    def setUp(self):
        super().setUp()
        selector_cache.clear()

    def load_profile(self, actions="", **kwargs):
        self.load_html(profile_html(actions, **kwargs))
        return wait_for_profile(self.page, URL, personalize=False).identity

    def assert_action_rejected(self, selector, identity=IDENTITY, **kwargs):
        try:
            accepted = is_target_action(
                self.page.locator(selector), self.page, identity, **kwargs
            )
        except TaskSkippedException as exc:
            self.assertEqual(exc.reason, "profile_identity_mismatch")
            self.assertFalse(exc.cooldown_eligible)
        else:
            self.assertFalse(accepted)

    def test_direct_generic_target_connect_is_clicked(self):
        identity = self.load_profile(
            '<button id="target" onclick="this.textContent=\'Pending\'">Connect</button>',
            before='<aside><button id="foreign">Connect</button></aside>',
        )
        self.assertTrue(
            try_heuristic_connect(self.page, HumanActions(self.page), identity)
        )
        self.assertEqual(self.page.locator("#target").inner_text(), "Pending")
        self.assertEqual(self.page.locator("#foreign").inner_text(), "Connect")
        self.assertEqual(
            detect_connection_state(self.page, identity), ConnectionState.PENDING
        )

    def test_icon_only_named_target_connect_is_accepted(self):
        identity = self.load_profile(
            '<button id="target" aria-label="Invite Jane Prospect to connect">+</button>'
        )
        self.assertTrue(
            is_target_action(self.page.locator("#target"), self.page, identity)
        )

    def test_disabled_hidden_and_nonaction_connect_nodes_are_rejected(self):
        identity = self.load_profile("""<button id="disabled" disabled>Connect</button>
            <button id="aria" aria-disabled="true">Connect</button>
            <button id="hidden" hidden>Connect</button><div id="prose">Connect</div>""")
        for selector in ("#disabled", "#aria", "#hidden", "#prose"):
            with self.subTest(selector=selector):
                self.assert_action_rejected(selector, identity)

    def test_foreign_sidebar_more_is_not_clicked(self):
        identity = self.load_profile(
            before="""<aside><button id="foreign"
            onclick="this.textContent='WRONG'">More</button></aside>"""
        )
        self.assertFalse(
            try_heuristic_connect(self.page, HumanActions(self.page), identity)
        )
        self.assertEqual(self.page.locator("#foreign").inner_text(), "More")

    def test_cache_skips_wrong_first_match_and_returns_target_locator(self):
        identity = self.load_profile(
            '<button class="connect" id="target">Connect</button>',
            before='<aside><button class="connect" id="foreign" aria-label="Invite Other Person to connect">Connect</button></aside>',
        )
        save_selector_to_cache(self.page, identity, ".connect")
        cached = get_cached_connect_button(self.page, identity)
        self.assertIsNotNone(cached)
        self.assertEqual(cached.get_attribute("id"), "target")
        self.assertTrue(is_target_action(cached, self.page, identity))

    def test_cache_does_not_keep_more_or_foreign_controls(self):
        identity = self.load_profile(
            '<button id="more">More</button>',
            after='<aside><button id="foreign">Connect</button></aside>',
        )
        save_selector_to_cache(self.page, identity, "#more")
        save_selector_to_cache(self.page, identity, "#foreign")
        self.assertIsNone(get_cached_connect_button(self.page, identity))

    def test_exact_slug_links_are_allowed_outside_topcard(self):
        identity = self.load_profile(
            after='<a id="exact" href="/preload/custom-invite/?vanityName=jane-prospect">Connect</a>'
        )
        self.assertTrue(
            is_target_action(self.page.locator("#exact"), self.page, identity)
        )

    def test_slug_prefix_is_never_an_exact_recipient(self):
        identity = self.load_profile(
            after="""
            <a id="prefix" href="/in/jane-prospect-other/">Connect</a>
            <a id="vanity" href="/preload/custom-invite/?vanityName=jane-prospect-other">Connect</a>"""
        )
        self.assert_action_rejected("#prefix", identity)
        self.assert_action_rejected("#vanity", identity)

    def test_deceptive_host_and_duplicate_recipient_query_are_rejected(self):
        identity = self.load_profile(
            after="""
            <a id="host" href="https://linkedin.com.evil.test/in/jane-prospect/">Connect</a>
            <a id="query" href="/preload/custom-invite/?vanityName=jane-prospect&amp;vanityName=foreign">Connect</a>"""
        )
        self.assert_action_rejected("#host", identity)
        self.assert_action_rejected("#query", identity)

    def test_encoded_exact_target_query_is_accepted(self):
        identity = self.load_profile(
            after='<a id="exact" href="/preload/custom-invite/?vanityName=jane%2Dprospect">Connect</a>'
        )
        self.assertTrue(
            is_target_action(self.page.locator("#exact"), self.page, identity)
        )

    def test_mismatched_aria_overrides_target_href(self):
        identity = self.load_profile(
            after='<a id="wrong" href="/preload/custom-invite/?vanityName=jane-prospect" aria-label="Invite Other Person to connect">Connect</a>'
        )
        self.assert_action_rejected("#wrong", identity)

    def test_mismatched_href_overrides_topcard_scope(self):
        identity = self.load_profile('<button id="target">Connect</button>')
        self.page.locator("#target").evaluate(
            "el => el.outerHTML = '<a id=target href=/in/foreign/>Connect</a>'"
        )
        self.assert_action_rejected("#target", identity)

    def test_mismatched_aria_overrides_topcard_scope(self):
        identity = self.load_profile('<button id="target">Connect</button>')
        self.page.locator("#target").evaluate(
            "el => el.setAttribute('aria-label', 'Invite Jane Prospect Other to connect')"
        )
        self.assert_action_rejected("#target", identity)

    def test_unknown_profile_has_no_generic_action_fallback(self):
        self.load_html(
            '<main><button id="target">Connect</button></main>',
            url="https://www.linkedin.com/feed/",
        )
        with self.assertRaises(TaskSkippedException):
            try_heuristic_connect(self.page, HumanActions(self.page), IDENTITY)

    def test_missing_identity_marker_aborts_cached_and_state_paths(self):
        self.load_html(
            '<main><section class="pv-top-card"><h1>Jane Prospect</h1><button>Connect</button></section></main>'
        )
        selector_cache["connect"] = "button"
        with self.assertRaises(TaskSkippedException):
            get_cached_connect_button(self.page, IDENTITY)
        with self.assertRaises(TaskSkippedException):
            detect_connection_state(self.page, IDENTITY)

    def test_sidebar_pending_and_degree_badges_do_not_change_target_state(self):
        identity = self.load_profile(
            "<button>Connect</button>",
            before="""
            <aside><h2>First Person</h2><span class="dist-value">1st</span><button>Pending</button></aside>
            <aside><h2>Second Person</h2><span class="distance-badge">1st degree</span><button>Pending</button></aside>""",
        )
        self.assertEqual(
            detect_connection_state(self.page, identity), ConnectionState.CONNECTABLE
        )

    def test_nested_foreign_card_does_not_supply_degree_or_pending(self):
        identity = self.load_profile(
            "<button>Connect</button>",
            inside="""
            <div><a href="/in/foreign/"><h2>Foreign Person</h2></a>
                <span class="dist-value">1st</span><button>Pending</button></div>""",
        )
        self.assertEqual(
            detect_connection_state(self.page, identity), ConnectionState.CONNECTABLE
        )

    def test_own_degree_badge_is_connected(self):
        identity = self.load_profile(
            "<button>Message</button>", inside='<span class="dist-value">1st</span>'
        )
        self.assertEqual(
            detect_connection_state(self.page, identity), ConnectionState.CONNECTED
        )

    def test_own_pending_named_link_is_pending(self):
        identity = self.load_profile(
            '<a href="/in/jane-prospect/" aria-label="Pending, click to withdraw invitation sent to Jane Prospect">Pending</a>'
        )
        self.assertEqual(
            detect_connection_state(self.page, identity), ConnectionState.PENDING
        )

    def test_following_without_degree_is_unknown(self):
        identity = self.load_profile("<button>Following</button>")
        self.assertEqual(
            detect_connection_state(self.page, identity), ConnectionState.UNKNOWN
        )

    def test_target_more_opens_and_clicks_unique_menu(self):
        identity = self.load_profile(
            """<button id="more" aria-label="More actions"
            onclick="document.querySelector('#menu').hidden=false">More</button>""",
            after="""
            <div id="menu" role="menu" hidden><button id="target" role="menuitem"
                onclick="document.querySelector('#topcard .pvs-profile-actions').innerHTML='<button>Pending</button>';this.parentElement.hidden=true">Connect</button></div>""",
        )
        self.assertTrue(
            try_heuristic_connect(self.page, HumanActions(self.page), identity)
        )
        self.assertEqual(
            detect_connection_state(self.page, identity), ConnectionState.PENDING
        )

    def test_unrelated_preexisting_menu_does_not_authorize_connect(self):
        identity = self.load_profile(
            '<button id="more">More</button>',
            after="""
            <div role="menu"><button id="foreign" onclick="this.textContent='WRONG'">Connect</button></div>""",
        )
        self.assertFalse(
            try_heuristic_connect(self.page, HumanActions(self.page), identity)
        )
        self.assertEqual(self.page.locator("#foreign").inner_text(), "Connect")
        self.assert_action_rejected("#foreign", identity)
        self.assert_action_rejected("#more", identity, allow_more=True)

    def test_multiple_opened_menus_are_ambiguous(self):
        identity = self.load_profile(
            """<button id="more"
            onclick="document.querySelectorAll('[role=menu]').forEach(el=>el.hidden=false)">More</button>""",
            after="""
            <div role="menu" hidden><button id="one">Connect</button></div>
            <div role="menu" hidden><button id="two">Connect</button></div>""",
        )
        self.assertFalse(
            try_heuristic_connect(self.page, HumanActions(self.page), identity)
        )
        self.assert_action_rejected("#one", identity, from_profile_menu=True)
        self.assert_action_rejected("#two", identity, from_profile_menu=True)

    def test_generic_menu_connect_is_not_cached_without_provenance(self):
        identity = self.load_profile(
            '<button id="more">More</button>',
            after='<div role="menu"><button id="menuconnect">Connect</button></div>',
        )
        save_selector_to_cache(self.page, identity, "#menuconnect")
        self.assertIsNone(get_cached_connect_button(self.page, identity))

    def test_nested_foreign_generic_connect_is_not_own_action(self):
        identity = self.load_profile(
            inside="""<div><a href="/in/foreign/"><h2>Foreign Person</h2></a>
            <button id="foreign">Connect</button></div>"""
        )
        self.assert_action_rejected("#foreign", identity)
        self.assertFalse(
            try_heuristic_connect(self.page, HumanActions(self.page), identity)
        )

    def test_drift_during_scroll_aborts_before_click(self):
        identity = self.load_profile("""<button id="target" style="margin-top:2000px"
            onclick="document.body.dataset.clicked='yes'">Connect</button>""")
        self.page.evaluate("""() => window.addEventListener('scroll', () => {
            document.querySelector('h1').textContent='Wrong Person';
        }, {once: true})""")
        with self.assertRaises(TaskSkippedException) as raised:
            try_heuristic_connect(self.page, HumanActions(self.page), identity)
        self.assertEqual(raised.exception.reason, "profile_identity_mismatch")
        self.assertIsNone(self.page.locator("body").get_attribute("data-clicked"))

    def test_cached_locator_is_revalidated_after_identity_drift(self):
        identity = self.load_profile('<button id="target">Connect</button>')
        save_selector_to_cache(self.page, identity, "#target")
        cached = get_cached_connect_button(self.page, identity)
        self.assertIsNotNone(cached)
        self.page.locator("h1").evaluate("el => el.textContent='Wrong Person'")
        with self.assertRaises(TaskSkippedException):
            is_target_action(cached, self.page, identity)

    def test_click_timeout_after_dispatch_never_clicks_another_candidate(self):
        identity = self.load_profile("""
            <button id="first" onclick="document.body.dataset.clicked=(document.body.dataset.clicked||'')+'first'">Connect</button>
            <button id="second" onclick="document.body.dataset.clicked=(document.body.dataset.clicked||'')+'second'">Connect</button>""")
        original_click = BrowserLocator.click

        def dispatch_then_timeout(locator, *args, **kwargs):
            original_click(locator, *args, **kwargs)
            raise BrowserTimeoutError(
                "Transport timed out after the click was dispatched"
            )

        with patch.object(BrowserLocator, "click", dispatch_then_timeout):
            with self.assertRaises(TaskSkippedException) as raised:
                try_heuristic_connect(self.page, HumanActions(self.page), identity)
        self.assertEqual(raised.exception.reason, "invite_not_confirmed")
        self.assertFalse(raised.exception.cooldown_eligible)
        self.assertEqual(
            self.page.locator("body").get_attribute("data-clicked"), "first"
        )

    def test_notification_scanner_uses_verified_identity(self):
        self.load_profile("<button>Pending</button>")
        task = NotificationReplyInviteScanner.__new__(NotificationReplyInviteScanner)
        task.page = self.page
        self.assertEqual(task._get_connection_state(URL), ConnectionState.PENDING)

    def test_notification_scanner_aborts_on_contradictory_topcard(self):
        self.load_html(
            profile_html("<button>Connect</button>").replace(
                'href="/in/jane-prospect/"', 'href="/in/other-person/"'
            )
        )
        task = NotificationReplyInviteScanner.__new__(NotificationReplyInviteScanner)
        task.page = self.page
        with self.assertRaises(TaskSkippedException) as raised:
            task._get_connection_state(URL)
        self.assertEqual(raised.exception.reason, "profile_identity_mismatch")


if __name__ == "__main__":
    unittest.main()
