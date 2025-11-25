import logging
from .base import BaseTask
from llm import generate_connection_message
from markdownify import markdownify as md

logger = logging.getLogger(__name__)


class InviteTask(BaseTask):
    def run(self, payload: dict):
        url = payload.get("url")
        if not url:
            raise ValueError("URL is required for invite task")

        try_personal_message = payload.get("try_personal_message", True)
        self.send_connection_request(url, try_personal_message)

    def get_profile_content(self):
        """Get the content of the profile page using Playwright."""
        try:
            # Wait for the main content to load
            self.page.wait_for_selector("main", state="attached", timeout=5000)

            # Get the HTML content of the main element
            if self.page.locator("main").count() > 0:
                html = self.page.locator("main").inner_html()
            else:
                html = self.page.content()

            return md(html)
        except Exception as e:
            logger.error(f"Error getting profile content: {e}")
            return ""

    def send_connection_request(self, url: str, try_personal_message: bool = True):
        """Send a connection request to the user."""
        logger.info(f"Sending connection request to {url}...")

        try:
            self.page.goto(url, timeout=60000, wait_until="domcontentloaded")

            self.page.wait_for_selector("h1", timeout=15000)
            person_name = self.page.locator("h1").text_content()

            connect_button_selector = f"button[aria-label='Invite {person_name} to connect']"
            try:
                self.page.wait_for_selector(connect_button_selector, timeout=5000, state="attached")
            except Exception:
                try:
                    connect_button_selector = f"div[aria-label='Invite {person_name} to connect']"
                    self.page.wait_for_selector(connect_button_selector, timeout=5000, state="attached")
                except Exception:
                    raise Exception("Can't find invite button, possibly already connected")

            connection_message = None

            if try_personal_message:
                profile_content = self.get_profile_content()
                if len(profile_content) > 0:
                    connection_message = generate_connection_message(profile_content)
                    if connection_message:
                        logger.info(
                            f"Generated connection message: {connection_message}"
                        )
                        self.page.locator("button[aria-label='More actions']").last.click()

            self.page.locator("button[aria-label='More actions']").last.click()
            self.page.locator(
                connect_button_selector
            ).last.click()

            if connection_message:
                try:
                    self.page.locator("button[aria-label='Add a note']").click()
                    self.page.wait_for_selector("#custom-message", timeout=1000)
                    self.page.fill("#custom-message", connection_message)
                    self.page.locator(
                        "button[aria-label='Send invitation']"
                    ).click()
                except Exception:
                    logger.warning("Possibly ran out of personalized connection messages, trying without")
                    return self.send_connection_request(url, False)

            else:
                self.page.locator(
                    "button[aria-label='Send without a note']"
                ).click()

            logger.info("Connection request sent successfully")

        except Exception as e:
            logger.error(f"Error sending connection request: {e}")
            raise e
