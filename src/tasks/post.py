import logging
from .base import BaseTask

logger = logging.getLogger(__name__)

class PostTask(BaseTask):
    def run(self, payload: dict):
        content = payload.get("content")
        if not content:
            raise ValueError("Content is required for post task")

        create_post_url = payload.get("create_post_url", None)
        if not create_post_url:
            raise ValueError("Create post URL is required for post task")
    
        self.page.goto(
            create_post_url,
            timeout=60000,
            wait_until="domcontentloaded",
        )

        self.page.wait_for_selector("div[role='textbox']", timeout=10000)
        self.human.type("div[role='textbox']", content)
        self.human.random_sleep(1.0, 3.0)
        self.human.click("button.share-actions__primary-action")

