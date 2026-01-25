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
    "div.pvs-profile-actions",
    "section.pv-top-card",
    "div.ph5:has(button[aria-label*='Connect'], button[aria-label*='More'])",
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

    def _check_invitation_error(self) -> Optional[str]:
        try:
            toast = self.page.locator("div.artdeco-toast-item").first
            toast.wait_for(state="visible", timeout=3000)
            text = toast.inner_text().lower()
            logger.debug(f"Toast content: {text}")
            
            if "invitation not sent" in text or "withdrawing" in text:
                return "withdrawal_cooldown"
            if "weekly invitation limit" in text or "limit" in text:
                return "weekly_limit_reached"
            if "error" in text or "failed" in text:
                return "unknown_error"
        except Exception:
            pass
        
        try:
            alert = self.page.locator("div[role='alert']:visible").first
            if alert.is_visible(timeout=500):
                text = alert.inner_text().lower()
                logger.debug(f"Alert content: {text}")
                if "invitation not sent" in text or "withdrawing" in text:
                    return "withdrawal_cooldown"
                if "limit" in text:
                    return "weekly_limit_reached"
        except Exception:
            pass
            
        return None

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

        self.human.random_sleep(2.0, 4.0)
        
        error = self._check_invitation_error()
        if error:
            logger.warning(f"Invitation failed: {error}")
            try:
                close_btn = self.page.locator("button[aria-label='Dismiss']")
                if close_btn.is_visible(timeout=500):
                    close_btn.click()
            except Exception:
                pass
            return {"status": "skipped", "reason": error}
        
        modal_still_open = False
        try:
            modal_still_open = self.page.locator("#custom-message").is_visible(timeout=500)
        except Exception:
            pass
        
        if modal_still_open:
            logger.warning("Modal still open after send - invitation may have failed")
            page_text = self.page.locator("body").inner_text().lower()
            if "invitation not sent" in page_text or "withdrawing" in page_text:
                return {"status": "skipped", "reason": "withdrawal_cooldown"}
            if "limit" in page_text:
                return {"status": "skipped", "reason": "weekly_limit_reached"}
        
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
                self.human.random_sleep(1.0, 2.0)
                
                error = self._check_invitation_error()
                if error:
                    logger.warning(f"Invitation blocked after heuristic click: {error}")
                    return {"status": "skipped", "reason": error}
                
                if self._wait_for_add_note():
                    return self._complete_connection(try_personal_message, url)

            cached_selector = get_cached_selector(self.page, "profile_card", "Connect")
            if cached_selector:
                logger.info(f"Using cached selector: {cached_selector}")
                try:
                    locator = self.page.locator(cached_selector).first
                    locator.scroll_into_view_if_needed()
                    self.human.random_sleep(0.3, 0.5)
                    locator.click(delay=100)
                    self.human.random_sleep(1.0, 2.0)
                    
                    error = self._check_invitation_error()
                    if error:
                        logger.warning(f"Invitation blocked after cached selector click: {error}")
                        return {"status": "skipped", "reason": error}
                    
                    if self._wait_for_add_note():
                        return self._complete_connection(try_personal_message, url)
                except Exception:
                    logger.debug("Cached selector failed")

            logger.info("Falling back to LLM for selector detection")
            
            previous_feedback = None
            
            for iteration in range(MAX_CONNECT_ITERATIONS):
                logger.info(f"LLM iteration {iteration + 1}/{MAX_CONNECT_ITERATIONS}")

                screenshot_bytes = self.page.screenshot()
                screenshot_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')

                container, container_name = self._get_action_container()
                container_html = container.inner_html()
                logger.debug(f"Using container: {container_name}")

                result = get_next_connect_action(screenshot_base64, container_html, previous_feedback)

                if result is None:
                    raise ValueError("LLM returned invalid response")

                selector = result.get("selector")
                expected_text = result.get("expected_text")
                reason = result.get("reason", "")
                
                logger.info(f"LLM response: selector={selector}, expected_text={expected_text}, reason={reason}")

                if selector is None:
                    logger.info(f"LLM says skip: {reason}")
                    return {"status": "skipped", "reason": reason}

                try:
                    all_matches = container.locator(selector).all()
                    
                    if not all_matches:
                        previous_feedback = f"Selector '{selector}' found no elements. Try a different selector."
                        logger.info(f"No elements found for selector: {selector}")
                        continue
                    
                    target_locator = None
                    
                    if expected_text:
                        for match in all_matches:
                            try:
                                if not match.is_visible(timeout=300):
                                    continue
                                text = match.inner_text().strip()
                                if text and (expected_text.lower() in text.lower() or text.lower() in expected_text.lower()):
                                    target_locator = match
                                    logger.info(f"Found matching element with text: {text}")
                                    break
                            except Exception:
                                continue
                        
                        if not target_locator:
                            previous_feedback = f"Element '{selector}' with text '{expected_text}' is NOT VISIBLE - it's likely inside a closed dropdown. Click the 'More' button first to open the dropdown."
                            logger.info(f"No visible element with expected text '{expected_text}' found among {len(all_matches)} matches (element may be in closed dropdown)")
                            continue
                    else:
                        visible_matches = [m for m in all_matches if m.is_visible(timeout=300)]
                        if not visible_matches:
                            previous_feedback = f"Element '{selector}' is NOT VISIBLE - it may be inside a closed dropdown. Click the 'More' button first."
                            logger.info(f"No visible elements found for selector: {selector}")
                            continue
                        target_locator = visible_matches[0]
                    
                    button_text = target_locator.inner_text().strip()
                    
                    target_locator.scroll_into_view_if_needed()
                    self.human.random_sleep(0.3, 0.5)
                    target_locator.click(delay=100)
                    self.human.random_sleep(1.0, 2.0)
                    
                    error = self._check_invitation_error()
                    if error:
                        logger.warning(f"Invitation blocked after clicking Connect: {error}")
                        return {"status": "skipped", "reason": error}
                    
                    save_selector_to_cache("profile_card", button_text, selector)
                    previous_feedback = None
                        
                except Exception as e:
                    previous_feedback = f"Selector '{selector}' failed to click: {str(e)[:100]}"
                    logger.info(f"Suggested selector not clickable: {e}")
                    continue

                if self._wait_for_add_note():
                    return self._complete_connection(try_personal_message, url)

            raise ValueError(f"Could not reach 'Add a note' after {MAX_CONNECT_ITERATIONS} iterations")

        except Exception as e:
            logger.error(f"Error sending connection request: {e}")
            raise e
