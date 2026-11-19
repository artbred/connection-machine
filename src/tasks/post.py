import logging
from .base import BaseTask
from notifications import send_notification

logger = logging.getLogger(__name__)

class PostTask(BaseTask):
    def _open_composer(self, url):
        self.validate_session()
        self.page.goto(url, timeout=60000, wait_until="domcontentloaded")
        self.page.wait_for_selector("div[role='textbox']", timeout=10000)

    def inspect_composer(self):
        self._open_composer("https://www.linkedin.com/feed/?shareActive=true")
        # Do not fill the editor: LinkedIn can autosave without a publish click.
        dialog = self.page.locator("[role='dialog']:visible, dialog[open]:visible").filter(
            has=self.page.locator("button.share-actions__primary-action:visible")
        )
        editor = dialog.locator("div[role='textbox']:visible")
        editor.wait_for(state="visible", timeout=10000)
        if dialog.count() != 1 or editor.count() != 1 or not editor.is_editable():
            raise RuntimeError("Could not verify an editable post composer")

    def run(self, payload: dict):
        content = payload.get("content")
        if not content:
            raise ValueError("Content is required for post task")

        create_post_url = payload.get("create_post_url", None)
        if not create_post_url:
            raise ValueError("Create post URL is required for post task")

        self._open_composer(create_post_url)
        self.human.type("div[role='textbox']", content)
        self.human.random_sleep(1.0, 3.0)
        self.human.click("button.share-actions__primary-action")

        # Send notification with task details
        content_preview = f'"{content[:50]}..."' if len(content) > 50 else f'"{content}"'
        send_notification(f"Create Post\nContent: {content_preview}")

