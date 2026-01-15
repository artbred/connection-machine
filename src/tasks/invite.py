import logging

from .base import BaseTask
from llm import generate_connection_message, get_connect_button_selector
from markdownify import markdownify as md
from notifications import send_notification

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

            self.human.random_sleep(1.0, 2.5)  # Simulating reading
            self.human.random_hover()  # Random movement while "reading"

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
            self.human.random_sleep(2.0, 4.0)

            self.page.wait_for_selector("h2", timeout=15000)
            user_section = self.page.locator("section.artdeco-card").first

            self.page.wait_for_selector("button[aria-label='More actions']", state='attached')
            self.page.locator("button[aria-label='More actions']").last.click(force=True)

            connect_button_selector = get_connect_button_selector(user_section.inner_html())
            if connect_button_selector is None:
                raise ValueError('Connect button selector is none')
            
            print(connect_button_selector)
            self.page.click(connect_button_selector, force=True)
            self.page.wait_for_selector("button[aria-label='Add a note']")
            self.page.click("button[aria-label='Add a note']")

            connection_message = None

            if try_personal_message:
                profile_content = self.get_profile_content()
                if len(profile_content) > 0:
                    connection_message = generate_connection_message(profile_content)
                    if connection_message:
                        logger.info(
                            f"Generated connection message: {connection_message}"
                        )

            self.page.wait_for_selector("#custom-message", timeout=1000)
            self.human.type("#custom-message", connection_message)

            send_btn = self.page.locator("button[aria-label='Send invitation']")
            self.human.click(send_btn)

            self.human.random_sleep(1.0, 3.0)
            logger.info("Connection request sent successfully")

            # Send notification with task details
            message_preview = (
                f'"{connection_message[:50]}..."'
                if connection_message and len(connection_message) > 50
                else (f'"{connection_message}"' if connection_message else "None")
            )
            send_notification(f"Send Invite to {url}\nMessage: {message_preview}")

        except Exception as e:
            logger.error(f"Error sending connection request: {e}")
            raise e
