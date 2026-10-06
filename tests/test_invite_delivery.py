"""Exercise recipient and send-once invariants on isolated browser pages."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from patchright.sync_api import Locator as BrowserLocator  # noqa: E402
from patchright.sync_api import TimeoutError as BrowserTimeoutError  # noqa: E402

from exceptions import TaskSkippedException  # noqa: E402
from human_actions import HumanActions  # noqa: E402
from invite_modal import find_invite_dialog  # noqa: E402
from invite_state import InviteStateStore  # noqa: E402
from linkedin_profile import wait_for_profile  # noqa: E402
from tasks import invite  # noqa: E402
from tests.browser_case import OfflineBrowserTestCase  # noqa: E402

TARGET = "https://www.linkedin.com/in/jane-prospect/"
NOTE = "Jane, your work on event-driven inventory systems caught my attention."
ABOUT = (
    "I build event-driven inventory systems and resilient streaming data platforms. "
    "My experience includes database operations, schema evolution, observability, "
    "and helping retail teams reconcile inventory changes reliably across regions."
)
PROFILE = f"""
<main><section class="pv-top-card">
  <h1><a href="/in/jane-prospect/">Jane Prospect</a></h1>
  <div class="text-body-medium break-words">Data engineer at Orchard Analytics</div>
  <div>5,000 followers<br>500+ connections</div>
  <button id="connect" aria-label="Invite Jane Prospect to connect"
          onclick="openInvite()">Connect</button>
</section><section><h2>About</h2><div>{ABOUT}</div></section>
<div><div role="heading">More profiles for you</div>
<a href="/in/stranger/">Alex Stranger</a><p>Satellite propulsion at Moonshot Labs</p></div>
</main>
"""
DIALOG = """
<div role="dialog" id="invite-dialog">
  <h2>Invite Jane Prospect to connect</h2>
  <textarea id="custom-message"></textarea>
  <button id="send" onclick="sendInvite()">Send</button>
</div>
"""


def fixture(dialog=DIALOG, *, already_open=True, confirm=True):
    return (
        PROFILE
        + (dialog if already_open else "")
        + """
<script>
document.body.dataset.sent = '[]';
document.body.dataset.chatSent = '0';
window.openInvite = () => document.body.insertAdjacentHTML('beforeend', DIALOG_HTML);
window.sendInvite = () => {
  const dialog = document.querySelector('#invite-dialog');
  const sent = JSON.parse(document.body.dataset.sent);
  sent.push({name: dialog.querySelector('h2').textContent,
             note: dialog.querySelector('textarea')?.value || ''});
  document.body.dataset.sent = JSON.stringify(sent);
  if (CONFIRM_SEND) {
    dialog.remove();
    const button = document.querySelector('#connect');
    button.textContent = 'Pending';
    button.setAttribute('aria-label', 'Pending');
  }
};
</script>
""".replace("DIALOG_HTML", json.dumps(dialog)).replace(
            "CONFIRM_SEND", "true" if confirm else "false"
        )
    )


class BrowserActions(HumanActions):
    """Real DOM interactions without production typing cadence in fixtures."""

    def __init__(self, page):
        self.page = page

    def random_sleep(self, *_):
        self.page.wait_for_timeout(1)

    def type(self, editor, text):
        editor.fill(text)

    def click(self, locator):
        locator.click()


class InviteDeliveryTest(OfflineBrowserTestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.history_path = Path(directory.name) / "history.json"
        self.state_path = Path(directory.name) / "state.json"
        history_patch = patch.object(invite, "INVITE_HISTORY_PATH", self.history_path)
        history_patch.start()
        self.addCleanup(history_patch.stop)
        environment_patch = patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "",
                "TELEGRAM_NOTIFICATIONS_URL": "",
                "TELEGRAM_CHAT_ID": "",
                "TELEGRAM_API_KEY": "",
                "INVITE_MIN_FOLLOWERS": "3000",
                "INVITE_REQUIRE_500_CONNECTIONS": "true",
            },
        )
        environment_patch.start()
        self.addCleanup(environment_patch.stop)

    def make_task(self, html):
        self.load_html(html, TARGET)
        task = invite.InviteTask(self.page)
        task.human = BrowserActions(self.page)
        task.invite_state = InviteStateStore(self.state_path)
        task._target_identity = wait_for_profile(
            self.page, TARGET, personalize=False
        ).identity
        return task

    def sent_records(self):
        # DOM attributes are shared between the page and Patchright's isolated
        # evaluation world; page-owned window globals are not.
        return json.loads(self.page.locator("body").get_attribute("data-sent"))

    def assert_nothing_sent(self):
        self.assertEqual(self.sent_records(), [])
        self.assertFalse(self.history_path.exists())

    def test_complete_flow_personalizes_only_owned_sections(self):
        task = self.make_task(fixture(already_open=False))

        def generate(content, name):
            self.assertEqual(name, "Jane Prospect")
            self.assertIn("inventory systems", content)
            self.assertNotIn("Moonshot", content)
            self.assertNotIn("Alex Stranger", content)
            return NOTE

        with patch.object(invite, "generate_connection_message", side_effect=generate):
            result = task.send_connection_request(TARGET)
        self.assertEqual(result, {"status": "pending", "message": NOTE})
        self.assertEqual(
            self.sent_records(),
            [{"name": "Invite Jane Prospect to connect", "note": NOTE}],
        )
        history = list(json.loads(self.history_path.read_text()).values())
        self.assertEqual(
            [(e["url"], e["status"], e["message"]) for e in history],
            [(TARGET, "pending", NOTE)],
        )

    def test_no_note_path_removes_preexisting_editor_text(self):
        dialog = DIALOG.replace(
            '<textarea id="custom-message"></textarea>',
            '<textarea id="custom-message">Someone else’s stale note</textarea>',
        )
        task = self.make_task(fixture(dialog, already_open=False))
        with patch.object(
            invite,
            "generate_connection_message",
            side_effect=AssertionError(
                "No generation is permitted on the explicit no-note path"
            ),
        ):
            result = task.send_connection_request(TARGET, try_personal_message=False)
        self.assertEqual(result, {"status": "pending", "message": None})
        self.assertEqual(self.sent_records()[0]["note"], "")

    def test_wrong_recipient_stops_before_generation_or_typing(self):
        task = self.make_task(fixture(DIALOG.replace("Jane Prospect", "Alex Stranger")))
        with (
            patch.object(
                invite,
                "generate_connection_message",
                side_effect=AssertionError(
                    "A foreign modal must never reach generation"
                ),
            ),
            self.assertRaises(TaskSkippedException) as caught,
        ):
            task._complete_connection(TARGET, ABOUT)
        self.assertEqual(caught.exception.reason, "modal_recipient_mismatch")
        self.assertEqual(self.page.locator("textarea").input_value(), "")
        self.assert_nothing_sent()

    def test_matching_name_cannot_override_wrong_modal_profile_link(self):
        dialog = DIALOG.replace("<h2>", '<a href="/in/stranger/">Jane Prospect</a><h2>')
        task = self.make_task(fixture(dialog))
        with self.assertRaises(TaskSkippedException) as caught:
            task._complete_connection(TARGET, "")
        self.assertEqual(caught.exception.reason, "modal_recipient_mismatch")
        self.assert_nothing_sent()

    def test_first_name_alone_does_not_identify_recipient(self):
        task = self.make_task(fixture(DIALOG.replace("Jane Prospect", "Jane")))
        with self.assertRaises(TaskSkippedException) as caught:
            task._complete_connection(TARGET, "")
        self.assertEqual(caught.exception.reason, "modal_recipient_mismatch")
        self.assert_nothing_sent()

    def test_exact_profile_link_binds_short_modal_name(self):
        dialog = DIALOG.replace("Jane Prospect", "Jane").replace(
            "<h2>", '<a href="/in/jane-prospect/?tracking=1">Jane</a><h2>'
        )
        task = self.make_task(fixture(dialog))
        result = task._complete_connection(TARGET, "")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(
            self.sent_records(), [{"name": "Invite Jane to connect", "note": ""}]
        )

    def test_chat_dialog_send_button_is_not_an_invitation(self):
        chat = '<div role="dialog"><h2>Message Jane Prospect</h2><textarea></textarea><button>Send</button></div>'
        task = self.make_task(fixture(chat))
        self.assertIsNone(find_invite_dialog(self.page, task._target_identity))
        with self.assertRaises(TaskSkippedException):
            task._get_send_invitation_button()
        self.assert_nothing_sent()

    def test_chat_before_invitation_does_not_steal_note_or_send(self):
        chat = '<div role="dialog"><h2>Messages</h2><textarea id="chat">Keep this draft</textarea><button onclick="document.body.dataset.chatSent = String(Number(document.body.dataset.chatSent) + 1)">Send</button></div>'
        task = self.make_task(fixture(chat + DIALOG))
        with patch.object(invite, "generate_connection_message", return_value=NOTE):
            task._complete_connection(TARGET, ABOUT)
        self.assertEqual(self.page.locator("#chat").input_value(), "Keep this draft")
        self.assertEqual(self.page.locator("body").get_attribute("data-chat-sent"), "0")
        self.assertEqual(self.sent_records()[0]["note"], NOTE)

    def test_two_invitation_dialogs_are_ambiguous(self):
        task = self.make_task(
            fixture(DIALOG + DIALOG.replace('id="invite-dialog"', 'id="other"'))
        )
        with self.assertRaises(TaskSkippedException) as caught:
            task._complete_connection(TARGET, "")
        self.assertEqual(caught.exception.reason, "modal_recipient_mismatch")
        self.assert_nothing_sent()

    def test_recipient_change_during_generation_aborts(self):
        task = self.make_task(fixture())

        def change_recipient(*_):
            self.page.locator("#invite-dialog h2").evaluate(
                "el => el.textContent = 'Invite Alex Stranger to connect'"
            )
            return NOTE

        with (
            patch.object(
                invite, "generate_connection_message", side_effect=change_recipient
            ),
            self.assertRaises(TaskSkippedException) as caught,
        ):
            task._complete_connection(TARGET, ABOUT)
        self.assertEqual(caught.exception.reason, "modal_recipient_mismatch")
        self.assert_nothing_sent()

    def test_profile_change_during_generation_aborts(self):
        task = self.make_task(fixture())

        def change_profile(*_):
            self.page.locator("h1").evaluate(
                "el => el.innerHTML = '<a href=\"/in/stranger/\">Alex Stranger</a>'"
            )
            return NOTE

        with (
            patch.object(
                invite, "generate_connection_message", side_effect=change_profile
            ),
            self.assertRaises(TaskSkippedException) as caught,
        ):
            task._complete_connection(TARGET, ABOUT)
        self.assertEqual(caught.exception.reason, "profile_identity_mismatch")
        self.assert_nothing_sent()

    def test_disabled_send_controls_fail_closed(self):
        for attribute in ("disabled", 'aria-disabled="true"', 'class="is-disabled"'):
            with self.subTest(attribute=attribute):
                task = self.make_task(
                    fixture(DIALOG.replace('id="send"', f'id="send" {attribute}'))
                )
                with self.assertRaises(TaskSkippedException) as caught:
                    task._complete_connection(TARGET, "")
                self.assertEqual(caught.exception.reason, "invite_not_confirmed")
                self.assert_nothing_sent()

    def test_note_readiness_rejects_extra_text(self):
        task = self.make_task(fixture())
        editor = self.page.locator("#custom-message")
        editor.fill("Stale prefix " + NOTE)
        self.assertFalse(task._invite_note_is_ready(editor, NOTE))
        editor.fill(NOTE)
        self.assertTrue(task._invite_note_is_ready(editor, NOTE))
        self.page.locator("#send").evaluate("el => el.remove()")
        with self.assertRaises(TaskSkippedException):
            task._invite_note_is_ready(editor, NOTE)

    def test_llm_more_flow_keeps_nested_menu_scope_bound(self):
        html = fixture(already_open=False)
        html = html.replace(
            '<button id="connect" aria-label="Invite Jane Prospect to connect"\n'
            '          onclick="openInvite()">Connect</button>',
            '<button id="more" onclick="document.querySelector(\'#menu-root\').hidden=false">'
            "More</button>",
        ).replace(
            "document.querySelector('#connect')", "document.querySelector('#more')"
        )
        html += """<div id="menu-root" class="artdeco-dropdown__content" hidden>
          <div role="menu"><button id="menu-connect"
            onclick="openInvite();document.querySelector('#menu-root').hidden=true">
            Connect</button></div></div>"""
        task = self.make_task(html)
        actions = [
            {"selector": "#more", "expected_text": "More"},
            {"selector": "#menu-connect", "expected_text": "Connect"},
        ]
        with (
            patch.object(invite, "try_heuristic_connect", return_value=False),
            patch.object(invite, "get_next_connect_action", side_effect=actions),
        ):
            result = task.send_connection_request(TARGET, try_personal_message=False)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(
            self.sent_records(),
            [{"name": "Invite Jane Prospect to connect", "note": ""}],
        )

    def test_vanished_menu_clears_its_action_authority(self):
        task = self.make_task(fixture(already_open=False))
        task._profile_menu_open = True
        task._get_action_container()
        self.assertFalse(task._profile_menu_open)
        self.page.locator("body").evaluate(
            "el => el.insertAdjacentHTML('beforeend', '<div role=\"menu\"><button id=\"foreign\">Connect</button></div>')"
        )
        scope, _ = task._get_action_container()
        self.assertEqual(scope.locator("#foreign").count(), 0)

    def test_send_timeout_after_dispatch_confirms_without_resending(self):
        task = self.make_task(fixture())
        original = BrowserLocator.click

        # Read the id before dispatch, because the real handler removes dialog.
        def uncertain_click(locator, *args, **kwargs):
            is_send = locator.get_attribute("id") == "send"
            original(locator, *args, **kwargs)
            if is_send:
                raise BrowserTimeoutError("Timeout after dispatch")

        with patch.object(BrowserLocator, "click", uncertain_click):
            result = task._complete_connection(TARGET, "")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(
            self.sent_records(),
            [{"name": "Invite Jane Prospect to connect", "note": ""}],
        )

    def test_unconfirmed_send_is_not_retried_or_accepted_from_generic_toast(self):
        task = self.make_task(
            fixture(confirm=False)
            + '<div class="artdeco-toast-item">Invitation sent</div>'
        )
        with self.assertRaises(TaskSkippedException) as caught:
            task._complete_connection(TARGET, "")
        self.assertEqual(caught.exception.reason, "invite_not_confirmed")
        with self.assertRaises(TaskSkippedException):
            task._complete_connection(TARGET, "")
        self.assertEqual(
            self.sent_records(),
            [{"name": "Invite Jane Prospect to connect", "note": ""}],
        )
        self.assertFalse(self.history_path.exists())

    def test_preflight_audience_failure_retains_explicit_retry_permission(self):
        task = self.make_task(
            fixture(already_open=False).replace("5,000 followers<br>", "")
        )
        with self.assertRaises(TaskSkippedException) as caught:
            task.run({"url": TARGET})
        self.assertEqual(caught.exception.reason, "audience_unavailable")
        self.assertTrue(caught.exception.retryable_preflight)
        self.assert_nothing_sent()

    def test_readiness_error_after_send_never_becomes_a_preflight_retry(self):
        task = self.make_task(fixture(already_open=False))
        with (
            patch.object(
                task,
                "_confirm_invitation_sent",
                side_effect=TaskSkippedException(
                    "profile_not_ready", cooldown_eligible=False
                ),
            ),
            self.assertRaises(TaskSkippedException) as caught,
        ):
            task.run({"url": TARGET, "try_personal_message": False})
        self.assertEqual(caught.exception.reason, "invite_not_confirmed")
        self.assertFalse(caught.exception.retryable_preflight)
        self.assertEqual(
            self.sent_records(),
            [{"name": "Invite Jane Prospect to connect", "note": ""}],
        )
        self.assertFalse(self.history_path.exists())


if __name__ == "__main__":
    unittest.main()
