import logging
from enum import Enum
from playwright.sync_api import Page

logger = logging.getLogger(__name__)


class ConnectionState(str, Enum):
    CONNECTABLE = "connectable"
    PENDING = "pending"
    CONNECTED = "connected"
    UNKNOWN = "unknown"


def _has_visible_connect_button(page: Page) -> bool:
    selectors = [
        "button[aria-label*='Connect']",
        "button[aria-label*='connect']",
        "button:has-text('Connect')",
    ]

    for selector in selectors:
        locator = page.locator(selector)
        for i in range(min(locator.count(), 10)):
            try:
                btn = locator.nth(i)
                if not btn.is_visible(timeout=300):
                    continue

                text = (btn.inner_text(timeout=300) or "").strip()
                aria_label = (btn.get_attribute("aria-label") or "").strip()
                combined = f"{text} {aria_label}".lower()
                if "connect" in combined and "disconnect" not in combined:
                    return True
            except Exception:
                continue

    return False


def detect_connection_state(page: Page) -> ConnectionState:
    try:
        pending_locator = page.locator("button:has-text('Pending')")
        if pending_locator.count() > 0 and pending_locator.first.is_visible():
            logger.info("Detected PENDING state - connection already sent")
            return ConnectionState.PENDING

        if _has_visible_connect_button(page):
            logger.info("Detected CONNECTABLE state - Connect control visible")
            return ConnectionState.CONNECTABLE
        
        profile_section = page.locator("section.artdeco-card").first
        try:
            section_text = profile_section.inner_text(timeout=1000).lower()
            if "1st" in section_text or "1st degree" in section_text:
                logger.info("Detected CONNECTED state - 1st degree connection")
                return ConnectionState.CONNECTED
        except Exception:
            pass

        following_btn = page.locator("button[aria-label*='Following']")
        if following_btn.count() > 0 and following_btn.first.is_visible():
            logger.info("Following button visible, but connection state is ambiguous")

        return ConnectionState.UNKNOWN
        
    except Exception as e:
        logger.warning(f"State detection failed: {e}")
        return ConnectionState.UNKNOWN
