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
        self.load_html(MEMBER_NAV, comment.FEED_URL)
        self._fixtures[comment.FEED_URL + "?shareActive=true"] = MEMBER_NAV + """
            <div role="dialog"><div role="textbox" contenteditable="true"
                oninput="document.body.dataset.changed='yes'">An existing draft</div>
            <button class="share-actions__primary-action"
                onclick="document.body.dataset.published='yes'">Post</button></div>"""
        run_dry_run(self.page, "post")
        self.assertEqual(self.page.locator("[role=textbox]").inner_text(), "An existing draft")
        self.assertIsNone(self.page.locator("body").get_attribute("data-changed"))
        self.assertIsNone(self.page.locator("body").get_attribute("data-published"))

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
        self._fixtures[url] = """<div role="textbox" contenteditable="true" style="min-height:32px"></div>
            <button class="share-actions__primary-action" onclick="document.body.dataset.sent=
                document.querySelector('[role=textbox]').textContent">Post</button>"""
        task = PostTask(self.page)
        with patch.object(task.human, "random_sleep"), patch("tasks.post.send_notification"):
            task.run({"create_post_url": url, "content": "Offline post"})
        self.assertEqual(self.page.locator("body").get_attribute("data-sent"), "Offline post")

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
