import base64
import logging

from .base import BaseTask
from llm import generate_connection_message, get_next_connect_action
from markdownify import markdownify as md
from notifications import send_notification

logger = logging.getLogger(__name__)

MAX_CONNECT_ITERATIONS = 5
ADD_NOTE_SELECTOR = "button[aria-label='Add a note']"


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

            # Iterative LLM loop to reach "Add a note" modal
            for iteration in range(MAX_CONNECT_ITERATIONS):
                logger.info(f"Connection flow iteration {iteration + 1}/{MAX_CONNECT_ITERATIONS}")

                # Capture current state
                screenshot_bytes = self.page.screenshot()
                screenshot_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')

                user_section = self.page.locator("body").first
                section_html = user_section.inner_html()

                # Ask LLM what to click
                result = get_next_connect_action(screenshot_base64, section_html)

                if result is None or "selector" not in result:
                    raise ValueError(f"LLM returned invalid response: {result}")

                selector = result["selector"]
                logger.info(f"LLM suggested selector: {selector}")

                try:
                    locator = self.page.locator(selector).first
                    self.human.click(locator, timeout=5000)
                    self.human.random_sleep(0.5, 1.5)
                except Exception:
                    logger.info("Suggested selector is not clickable")

                # Check if "Add a note" button appeared
                try:
                    self.page.wait_for_selector(ADD_NOTE_SELECTOR, timeout=2000)
                    logger.info("'Add a note' button detected, exiting loop")
                    break
                except Exception:
                    logger.debug("'Add a note' not yet visible, continuing...")
            else:
                raise ValueError(f"Could not reach 'Add a note' after {MAX_CONNECT_ITERATIONS} iterations")

            # Click "Add a note" to open the message modal
            self.human.click(ADD_NOTE_SELECTOR)

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
