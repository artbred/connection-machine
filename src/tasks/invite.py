import base64
import logging
from typing import Optional, Tuple

from playwright.sync_api import Locator

from .base import BaseTask
from llm import generate_connection_message, get_next_connect_action
from markdownify import markdownify as md
from notifications import send_notification
from connection_state import detect_connection_state, ConnectionState
from connect_heuristics import try_heuristic_connect, get_cached_selector, save_selector_to_cache

logger = logging.getLogger(__name__)

MAX_CONNECT_ITERATIONS = 5
ADD_NOTE_SELECTOR = "button[aria-label='Add a note']"

PROFILE_CONTAINER_SELECTORS = [
    "section.artdeco-card:has(button)",
    "main section:has(button)",
    "div.scaffold-layout__main",
    "main",
]

DROPDOWN_SELECTOR = "div.artdeco-dropdown__content:visible, div[role='menu']:visible"


class InviteTask(BaseTask):
    def run(self, payload: dict):
        url = payload.get("url")
        if not url:
            raise ValueError("URL is required for invite task")

        self.validate_session()

        try_personal_message = payload.get("try_personal_message", True)
        self.send_connection_request(url, try_personal_message)

    def get_profile_content(self) -> str:
        try:
            self.page.wait_for_selector("main", state="attached", timeout=5000)

            self.human.random_sleep(1.0, 2.5)
            self.human.random_hover()

            if self.page.locator("main").count() > 0:
                html = self.page.locator("main").inner_html()
            else:
                html = self.page.content()

            return md(html)
        except Exception as e:
            logger.error(f"Error getting profile content: {e}")
            return ""

    def _wait_for_add_note(self, timeout: int = 2000) -> bool:
        try:
            self.page.wait_for_selector(ADD_NOTE_SELECTOR, timeout=timeout)
            logger.info("'Add a note' button detected")
            return True
        except Exception:
            return False

    def _get_action_container(self) -> Tuple[Locator, str]:
        dropdown = self.page.locator(DROPDOWN_SELECTOR).first
        try:
            if dropdown.is_visible(timeout=300):
                return dropdown, "dropdown"
        except Exception:
            pass
        
        for selector in PROFILE_CONTAINER_SELECTORS:
            try:
                container = self.page.locator(selector).first
                if container.is_visible(timeout=300):
                    return container, selector
            except Exception:
                continue
        
        return self.page.locator("body").first, "body"

    def _complete_connection(self, try_personal_message: bool, url: str) -> dict:
        self.human.click(ADD_NOTE_SELECTOR)

        connection_message: Optional[str] = None

        if try_personal_message:
            profile_content = self.get_profile_content()
            if len(profile_content) > 0:
                connection_message = generate_connection_message(profile_content)
                if connection_message:
                    logger.info(f"Generated connection message: {connection_message}")

        self.page.wait_for_selector("#custom-message", timeout=1000)
        
        if connection_message:
            self.human.type("#custom-message", connection_message)

        send_btn = self.page.locator("button[aria-label='Send invitation']")
        self.human.click(send_btn)

        self.human.random_sleep(1.0, 3.0)
        logger.info("Connection request sent successfully")

        message_preview = (
            f'"{connection_message[:50]}..."'
            if connection_message and len(connection_message) > 50
            else (f'"{connection_message}"' if connection_message else "None")
        )
        send_notification(f"Send Invite to {url}\nMessage: {message_preview}")

        return {"status": "sent", "message": connection_message}

    def send_connection_request(self, url: str, try_personal_message: bool = True) -> dict:
        logger.info(f"Sending connection request to {url}...")

        try:
            self.page.goto(url, timeout=60000, wait_until="domcontentloaded")
            self.human.random_sleep(2.0, 4.0)
            self.page.wait_for_selector("h2", timeout=15000)

            state = detect_connection_state(self.page)
            
            if state == ConnectionState.PENDING:
                logger.info("Connection already pending, skipping")
                return {"status": "skipped", "reason": "already_pending"}
            
            if state == ConnectionState.CONNECTED:
                logger.info("Already connected, skipping")
                return {"status": "skipped", "reason": "already_connected"}

            if try_heuristic_connect(self.page, self.human):
                logger.info("Connected via heuristics (no LLM needed)")
                self.human.random_sleep(0.5, 1.0)
                if self._wait_for_add_note():
                    return self._complete_connection(try_personal_message, url)

            cached_selector = get_cached_selector(self.page, "profile_card", "Connect")
            if cached_selector:
                logger.info(f"Using cached selector: {cached_selector}")
                try:
                    self.human.click(self.page.locator(cached_selector).first)
                    self.human.random_sleep(0.5, 1.0)
                    if self._wait_for_add_note():
                        return self._complete_connection(try_personal_message, url)
                except Exception:
                    logger.debug("Cached selector failed")

            logger.info("Falling back to LLM for selector detection")
            
            for iteration in range(MAX_CONNECT_ITERATIONS):
                logger.info(f"LLM iteration {iteration + 1}/{MAX_CONNECT_ITERATIONS}")

                screenshot_bytes = self.page.screenshot()
                screenshot_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')

                container, container_name = self._get_action_container()
                container_html = container.inner_html()
                logger.debug(f"Using container: {container_name}")

                result = get_next_connect_action(screenshot_base64, container_html)

                if result is None:
                    raise ValueError("LLM returned invalid response")

                selector = result.get("selector")
                reason = result.get("reason", "")
                
                logger.info(f"LLM response: selector={selector}, reason={reason}")

                if selector is None:
                    logger.info(f"LLM says skip: {reason}")
                    return {"status": "skipped", "reason": reason}

                try:
                    locator = container.locator(selector).first
                    button_text = locator.inner_text().strip()
                    
                    self.human.click(locator, timeout=5000)
                    self.human.random_sleep(0.5, 1.5)
                    
                    save_selector_to_cache("profile_card", button_text, selector)
                        
                except Exception as e:
                    logger.info(f"Suggested selector not clickable: {e}")
                    continue

                if self._wait_for_add_note():
                    return self._complete_connection(try_personal_message, url)

            raise ValueError(f"Could not reach 'Add a note' after {MAX_CONNECT_ITERATIONS} iterations")

        except Exception as e:
            logger.error(f"Error sending connection request: {e}")
            raise e
