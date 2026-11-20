"""Exercise the non-invite task controls on intercepted pages, without an account."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests.browser_case import OfflineBrowserTestCase

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from main import run_dry_run  # noqa: E402
from tasks import comment  # noqa: E402
from tasks.notification_reply_invites import (  # noqa: E402
    NOTIFICATIONS_URL,
    NotificationReplyInviteScanner,
)
from tasks.post import PostTask  # noqa: E402

MEMBER_NAV = '<nav><a href="/mynetwork/">My Network</a></nav>'
PERMALINK = "https://www.linkedin.com/feed/update/urn:li:activity:123/"
FEED = f"""<!doctype html><main><article role="listitem">
    Ada Example
    <p>Our team measures queue latency during maintenance to identify where work accumulates before a deployment.</p>
    <button onclick="document.querySelector('#composer').hidden=false">Comment</button>
    <div id="composer" hidden>
      <div role="textbox" aria-label="Text editor for creating comment" contenteditable="true" style="min-height:32px"></div>
      <button onclick="const e=document.querySelector('[role=textbox]');
          document.body.dataset.sent=e.textContent; e.textContent=''">Post</button>
    </div>
    <button aria-label="Open control menu for post" onclick="document.querySelector('[role=menuitem]').hidden=false">More</button>
    <div role="menuitem" hidden onclick="navigator.clipboard.writeText('{PERMALINK}')">Copy link to post</div>
</article></main>"""


class TaskBrowserTests(OfflineBrowserTestCase):
    def test_post_dry_run_leaves_existing_draft_untouched(self):
        self.load_html(MEMBER_NAV + """
            <button onclick="location.href='/feed/?shareActive=true'">Start a post</button>
            """, comment.FEED_URL)
        self._fixtures[comment.FEED_URL + "?shareActive=true"] = MEMBER_NAV + """
            <div role="dialog"><div role="textbox" contenteditable="true"
                oninput="document.body.dataset.changed='yes'">An existing draft</div>
            <button class="share-actions__primary-action"
                onclick="document.body.dataset.published='yes'">Post</button></div>"""
        run_dry_run(self.page, "post")
        self.assertEqual(self.page.locator("[role=textbox]").inner_text(), "An existing draft")
        self.assertIsNone(self.page.locator("body").get_attribute("data-changed"))
        self.assertIsNone(self.page.locator("body").get_attribute("data-published"))

        with patch("tasks.post.send_notification") as notify:
            with self.assertRaisesRegex(RuntimeError, "draft"):
                PostTask(self.page).run({
                    "create_post_url": comment.FEED_URL + "?shareActive=true",
                    "content": "Do not replace the draft",
                })
            notify.assert_not_called()
        self.assertEqual(self.page.locator("[role=textbox]").inner_text(), "An existing draft")
        self.assertIsNone(self.page.locator("body").get_attribute("data-changed"))
        self.assertIsNone(self.page.locator("body").get_attribute("data-published"))

    def test_modern_post_waits_for_scoped_editor_and_publishes_only_on_run(self):
        url = "https://www.linkedin.com/sharing/compose"
        self.load_html(MEMBER_NAV + """
            <button onclick="location.href='/sharing/compose'">Start a post</button>
            """, comment.FEED_URL)
        self._fixtures[url] = MEMBER_NAV + """
            <button onclick="document.body.dataset.wrong='yes'">Post</button>
            <div role="textbox" contenteditable="true">Outside draft</div>
            <div role="dialog">
              <div role="textbox" contenteditable="true">Another editor</div>
              <div data-testid="ui-core-tiptap-text-editor-wrapper"></div>
              <template><div role="textbox" contenteditable="true" style="min-height:32px"
                  oninput="document.body.dataset.changed='yes';
                           document.querySelector('[data-view-name=share-post]').disabled=false"
                  ><p><br></p></div></template>
              <button data-view-name="share-post" disabled
                  onclick="document.body.dataset.sent=document.querySelector('[data-testid] div').innerText;
                           this.closest('[role=dialog]').remove()">Post</button>
            </div>
            <script>setTimeout(() => document.querySelector('[data-testid]')
                .append(document.querySelector('template').content.cloneNode(true)), 200)</script>"""
        run_dry_run(self.page, "post")
        self.assertEqual(self.page.locator("[data-testid] [role=textbox]").count(), 1)
        self.assertIsNone(self.page.locator("body").get_attribute("data-changed"))
        self.assertIsNone(self.page.locator("body").get_attribute("data-sent"))

        with patch("tasks.post.send_notification") as notify:
            PostTask(self.page).run({"create_post_url": url, "content": "Modern post"})
            notify.assert_called_once()
        self.assertEqual(self.page.locator("body").get_attribute("data-sent"), "Modern post")
        self.assertEqual(self.page.locator("[role=textbox]").inner_text(), "Outside draft")
        self.assertIsNone(self.page.locator("body").get_attribute("data-wrong"))

    def test_comment_dry_run_does_not_notify_or_store_history(self):
        self.load_html(MEMBER_NAV + FEED, comment.FEED_URL)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(comment, "COMMENT_HISTORY_PATH", Path(directory) / "history.json"),
            patch.dict(os.environ, OPENROUTER_API_KEY="offline"),
            patch.object(comment, "generate_feed_comment", return_value={"comment": "Fixture preview"}),
            patch.object(comment, "send_notification", side_effect=AssertionError("Preview notified")),
            patch("human_actions.HumanActions.random_sleep"),
        ):
            run_dry_run(self.page, "comment")
            self.assertTrue(self.page.locator("#composer").is_hidden())
            self.assertIsNone(self.page.locator("body").get_attribute("data-sent"))
            self.assertFalse(comment.COMMENT_HISTORY_PATH.exists())

    def test_notification_sdui_uses_the_linked_headline_not_quoted_content(self):
        def row(key, headline, extra=""):
            return f"""<div><a href="/in/{key}/"><img alt="View profile"></a>
                <div hidden id="{key}"><strong>{key}</strong> {headline}</div>
                <p>Someone else liked your comment.</p>{extra}
                <a href="/feed/update/fixture" aria-labelledby="{key}"></a></div>"""

        self.load_html('<main><div data-testid="lazy-column" data-component-type="LazyColumn">'
                       + row("wrong", "posted an update.")
                       + row("quoted", "liked your post about how someone loved your comment.")
                       + row("ambiguous", "liked your comment.", '<a href="/in/second/">Other</a>')
                       + row("ada", "replied to your comment.") + '</div></main>', NOTIFICATIONS_URL)
        candidates = NotificationReplyInviteScanner(self.page)._extract_reply_candidates()
        self.assertEqual([(c["profile_url"], c["text"]) for c in candidates],
                         [("https://www.linkedin.com/in/ada/", "ada replied to your comment.")])

    def test_notification_dry_run_never_opens_the_queue(self):
        self.load_html(MEMBER_NAV, comment.FEED_URL)
        self._fixtures[NOTIFICATIONS_URL] = MEMBER_NAV + """
            <ul><li><a href="/in/ada/">Ada Example</a> replied to your comment.</li></ul>"""
        with (
            patch("db.SessionLocal", side_effect=AssertionError("Preview opened the queue")),
            patch.object(NotificationReplyInviteScanner, "_save_state", side_effect=AssertionError),
            patch("human_actions.HumanActions.random_sleep"),
        ):
            run_dry_run(self.page, "notifications")
        self.assertEqual(self.page.url, NOTIFICATIONS_URL)

    def test_post_composer_submits_requested_content(self):
        url = "https://www.linkedin.com/fixture/composer/"
        self._fixtures[url] = """
            <div role="textbox" contenteditable="true">Unrelated</div>
            <button onclick="document.body.dataset.wrong='yes'">Post</button>
            <form onsubmit="return false">
              <div role="textbox" contenteditable="true" style="min-height:32px"
                   oninput="this.nextElementSibling.disabled=false"><p><br></p></div>
              <button class="share-actions__primary-action" disabled
                      onclick="document.body.dataset.sent=this.previousElementSibling.innerText;
                               this.form.remove()">Post</button>
            </form>"""
        with patch("tasks.post.send_notification") as notify:
            PostTask(self.page).run({"create_post_url": url, "content": "Offline post"})
            notify.assert_called_once()
        self.assertIsNone(self.page.locator("body").get_attribute("data-wrong"))
        self.assertEqual(self.page.locator("[role=textbox]").inner_text(), "Unrelated")
        self.assertEqual(self.page.locator("body").get_attribute("data-sent"), "Offline post")

    def test_post_refuses_duplicate_submit_before_input(self):
        url = "https://www.linkedin.com/feed/?shareActive=true"
        self._fixtures[url] = """
            <div role="dialog">
              <div role="textbox" contenteditable="true" style="min-height:32px"
                   oninput="document.body.dataset.changed='yes'"></div>
              <button class="share-actions__primary-action">Post</button><button>Post</button>
            </div>"""
        with self.assertRaisesRegex(RuntimeError, "unique"):
            PostTask(self.page).run({"create_post_url": url, "content": "Must not type"})
        self.assertIsNone(self.page.locator("body").get_attribute("data-changed"))

    def test_feed_dry_run_then_submit(self):
        self._fixtures[comment.FEED_URL] = FEED
        # Keep the developer's OS clipboard untouched; grant_permissions still runs in WebKit.
        self.page.add_init_script("""let copied = '';
            Object.defineProperty(navigator, 'clipboard', {value: {
                writeText: async text => {copied = text}, readText: async () => copied
            }});""")
        task = comment.FeedCommentTask(self.page)
        reply = "Which queue has the longest wait?"
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(comment, "COMMENT_HISTORY_PATH", Path(directory) / "history.json"),
            patch.dict(os.environ, OPENROUTER_API_KEY="offline"),
            patch.object(comment, "generate_feed_comment", return_value={"comment": reply}),
            patch.object(comment, "send_notification"),
            patch.object(task.human, "random_sleep"),
        ):
            preview = task.run({"dry_run": True})
            self.assertEqual(preview["comment"], reply)
            self.assertTrue(self.page.locator("#composer").is_hidden())
            self.assertFalse(comment.COMMENT_HISTORY_PATH.exists())
            sent = task.run({"dry_run": False})
            self.assertEqual(self.page.locator("body").get_attribute("data-sent"), reply)
            history = json.loads(comment.COMMENT_HISTORY_PATH.read_text())
            self.assertEqual(history[sent["post_key"]]["comment"], reply)
            self.assertEqual(history[sent["post_key"]]["post_href"], PERMALINK)

    def test_notification_scan_selects_comment_engagement(self):
        self._fixtures[NOTIFICATIONS_URL] = """<ul>
            <li><a href="/in/other/">Another Example</a> liked your post.</li>
            <li><a href="/in/ada/?tracking=abc">Ada Example</a> replied to your comment.</li>
            </ul>"""
        task = NotificationReplyInviteScanner(self.page)
        with patch.object(task.human, "random_sleep"):
            candidate, = task.find_reply_candidates(max_candidates=1)
        self.assertEqual(candidate["name"], "Ada Example")
        self.assertEqual(candidate["profile_url"], "https://www.linkedin.com/in/ada/")
