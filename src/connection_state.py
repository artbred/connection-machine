import logging
from enum import Enum
from playwright.sync_api import Page

logger = logging.getLogger(__name__)


class ConnectionState(str, Enum):
    CONNECTABLE = "connectable"
    PENDING = "pending"
    CONNECTED = "connected"
    UNKNOWN = "unknown"


def detect_connection_state(page: Page) -> ConnectionState:
    try:
        pending_locator = page.locator("button:has-text('Pending')")
        if pending_locator.count() > 0 and pending_locator.first.is_visible():
            logger.info("Detected PENDING state - connection already sent")
            return ConnectionState.PENDING
        
        following_btn = page.locator("button[aria-label*='Following']")
        if following_btn.count() > 0 and following_btn.first.is_visible():
            logger.info("Detected CONNECTED state - Following button visible")
            return ConnectionState.CONNECTED
        
        profile_section = page.locator("section.artdeco-card").first
        try:
            section_text = profile_section.inner_text(timeout=1000).lower()
            if "1st" in section_text or "1st degree" in section_text:
                logger.info("Detected CONNECTED state - 1st degree connection")
                return ConnectionState.CONNECTED
        except Exception:
            pass
        
        connect_buttons = page.locator("button").filter(has_text="Connect")
        for i in range(min(connect_buttons.count(), 5)):
            try:
                btn = connect_buttons.nth(i)
                if btn.is_visible(timeout=300):
                    text = btn.inner_text(timeout=300).strip()
                    if text == "Connect":
                        logger.info("Detected CONNECTABLE state - Connect button found")
                        return ConnectionState.CONNECTABLE
            except Exception:
                continue
        
        more_locator = page.locator("button[aria-label*='More actions']")
        if more_locator.count() > 0 and more_locator.first.is_visible():
            logger.info("Detected CONNECTABLE state - More actions available")
            return ConnectionState.CONNECTABLE
        
        return ConnectionState.UNKNOWN
        
    except Exception as e:
        logger.warning(f"State detection failed: {e}")
        return ConnectionState.UNKNOWN
