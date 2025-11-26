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
            self.human.random_sleep(2.0, 4.0)  # Wait a bit after load

            self.page.wait_for_selector("h1", timeout=15000)
            person_name = self.page.locator("h1").text_content()

            connect_button_selector = (
                f"button[aria-label='Invite {person_name} to connect']"
            )
            try:
                self.page.wait_for_selector(
                    connect_button_selector, timeout=5000, state="attached"
                )
            except Exception:
                try:
                    connect_button_selector = (
                        f"div[aria-label='Invite {person_name} to connect']"
                    )
                    self.page.wait_for_selector(
                        connect_button_selector, timeout=5000, state="attached"
                    )
                except Exception:
                    raise Exception(
                        "Can't find invite button, possibly already connected"
                    )

            connection_message = None

            if try_personal_message:
                profile_content = self.get_profile_content()
                if len(profile_content) > 0:
                    connection_message = generate_connection_message(profile_content)
                    if connection_message:
                        logger.info(
                            f"Generated connection message: {connection_message}"
                        )
                        # Close "More actions" if it was opened/interacted with?
                        # Actually the original code opened it here oddly twice in different branches
                        # Just ensuring menu is handled if needed
                        # For now keeping flow similar but with human clicks

            # Logic in original was: click 'More actions', then click 'connect_button'.
            # But 'connect_button' is usually on the main card or in 'More actions'.
            # If it's in 'More actions', we need to open it.
            # The selector search implies we found the button. If it's visible, we click it.
            # If not visible (inside menu), we might need to click "More actions" first.

            # Simplified flow assumption based on original code structure:
            # It seems the original code tried to click "More actions" blindly?
            # self.page.locator("button[aria-label='More actions']").last.click()

            # Let's try to locate the connect button. If not visible, try opening "More actions".
            connect_btn = self.page.locator(connect_button_selector).last
            if not connect_btn.is_visible():
                more_actions = self.page.locator(
                    "button[aria-label='More actions']"
                ).last
                if more_actions.is_visible():
                    self.human.click(more_actions)
                    self.human.random_sleep(0.5, 1.0)

            self.human.click(connect_btn)
            self.human.random_sleep(0.5, 1.0)

            if connection_message:
                try:
                    add_note_btn = self.page.locator("button[aria-label='Add a note']")
                    self.human.click(add_note_btn)

                    self.page.wait_for_selector("#custom-message", timeout=1000)
                    self.human.type("#custom-message", connection_message)

                    send_btn = self.page.locator("button[aria-label='Send invitation']")
                    self.human.click(send_btn)
                except Exception:
                    logger.warning(
                        "Possibly ran out of personalized connection messages, trying without"
                    )
                    # Recursively try without message - ensure we don't infinite loop if something else breaks
                    # But here we just want to close/cancel and retry without msg?
                    # Or just click "Send without note" if the previous steps failed?
                    # Safest is to return and retry with flag False
                    return self.send_connection_request(url, False)

            else:
                send_without_note_btn = self.page.locator(
                    "button[aria-label='Send without a note']"
                )
                if send_without_note_btn.is_visible():
                    self.human.click(send_without_note_btn)
                else:
                    # Sometimes "Send" is the button if no note option was presented differently?
                    # Fallback to looking for just "Send"
                    self.human.click("button[aria-label='Send now']")

            self.human.random_sleep(1.0, 3.0)
            logger.info("Connection request sent successfully")

        except Exception as e:
            logger.error(f"Error sending connection request: {e}")
            raise e
