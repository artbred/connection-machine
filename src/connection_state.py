import logging
import re
from enum import Enum
from typing import Any
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import Page
from playwright.sync_api import Locator

logger = logging.getLogger(__name__)

CONNECT_WORD_RE = re.compile(r"\bconnect\b")


class ConnectionState(str, Enum):
    CONNECTABLE = "connectable"
    PENDING = "pending"
    CONNECTED = "connected"
    UNKNOWN = "unknown"


PRIMARY_PROFILE_SCOPE_SELECTORS = [
    "div.pvs-profile-actions",
    "section.pv-top-card",
    "div.ph5:has(button[aria-label*='Connect'], button[aria-label*='More'])",
    "section.artdeco-card",
]


def _get_primary_profile_scope(page: Page) -> Locator | None:
    for selector in PRIMARY_PROFILE_SCOPE_SELECTORS:
        locator = page.locator(selector)
        if locator.count() == 0:
            continue

        candidate = locator.first
        try:
            if candidate.is_visible(timeout=300):
                return candidate
        except Exception:
            continue

    return None


def _current_profile_slug(page: Page) -> str:
    parsed = urlparse(page.url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "in":
        return parts[1].lower()
    return ""


def _href_targets_profile(href: str, profile_slug: str) -> bool:
    if not profile_slug:
        return True

    parsed = urlparse(href)
    query = {key.lower(): value for key, value in parse_qs(parsed.query).items()}
    vanity_names = [value.lower() for value in query.get("vanityname", [])]
    return profile_slug in vanity_names or f"/in/{profile_slug}" in parsed.path.lower()


def _has_visible_connect_button(
    scope: Any,
    profile_slug: str = "",
    *,
    allow_text_only: bool = True,
) -> bool:
    selectors = [
        "a[href*='/preload/custom-invite/']",
        "a[aria-label*='Connect' i]",
        "a:has-text('Connect')",
        "button[aria-label*='Connect']",
        "button[aria-label*='connect']",
        "button:has-text('Connect')",
    ]

    for selector in selectors:
        locator = scope.locator(selector)
        for i in range(min(locator.count(), 10)):
            try:
                btn = locator.nth(i)
                if not btn.is_visible(timeout=300):
                    continue

                text = (btn.inner_text(timeout=300) or "").strip()
                aria_label = (btn.get_attribute("aria-label") or "").strip()
                href = (btn.get_attribute("href") or "").lower()
                combined = f"{text} {aria_label}".lower()
                if "disconnect" in combined or "disconnect" in href:
                    continue
                if "/preload/custom-invite/" in href:
                    if _href_targets_profile(href, profile_slug):
                        return True
                    continue
                if allow_text_only and CONNECT_WORD_RE.search(combined):
                    return True
            except Exception:
                continue

    return False


def _has_visible_pending_button(
    scope: Any,
    profile_slug: str = "",
    *,
    allow_text_only: bool = True,
) -> bool:
    selectors = [
        "a[aria-label*='Pending' i]",
        "a[aria-label*='Withdraw' i]",
        "a:has-text('Pending')",
        "a:has-text('Withdraw')",
        "button[aria-label*='Pending']",
        "button[aria-label*='pending']",
        "button[aria-label*='Withdraw']",
        "button[aria-label*='withdraw']",
        "button:has-text('Pending')",
        "button:has-text('Withdraw')",
        "[role='button'][aria-label*='Pending' i]",
        "[role='button'][aria-label*='Withdraw' i]",
        "[role='button']:has-text('Pending')",
        "[role='button']:has-text('Withdraw')",
    ]

    for selector in selectors:
        locator = scope.locator(selector)
        for i in range(min(locator.count(), 10)):
            try:
                btn = locator.nth(i)
                if not btn.is_visible(timeout=300):
                    continue

                text = (btn.inner_text(timeout=300) or "").strip()
                aria_label = (btn.get_attribute("aria-label") or "").strip()
                href = (btn.get_attribute("href") or "").lower()
                combined = f"{text} {aria_label}".lower()
                if (
                    profile_slug
                    and href
                    and not _href_targets_profile(href, profile_slug)
                ):
                    continue
                if not allow_text_only and not href:
                    continue
                if "pending" in combined or "withdraw" in combined:
                    return True
            except Exception:
                continue

    return False


def _has_connected_marker(scope: Locator) -> bool:
    try:
        section_text = scope.inner_text(timeout=1000).lower()
    except Exception:
        return False

    return "1st degree" in section_text or " 1st" in f" {section_text}"


def _has_visible_following_button(scope: Page | Locator) -> bool:
    selectors = [
        "button[aria-label*='Following']",
        "button[aria-label*='following']",
        "button:has-text('Following')",
    ]

    for selector in selectors:
        locator = scope.locator(selector)
        try:
            if locator.count() > 0 and locator.first.is_visible(timeout=300):
                return True
        except Exception:
            continue

    return False


def resolve_connection_state(
    has_pending: bool,
    has_connected_marker: bool,
    has_connect: bool,
    has_following: bool,
) -> ConnectionState:
    if has_pending:
        return ConnectionState.PENDING

    if has_connected_marker:
        return ConnectionState.CONNECTED

    if has_connect:
        return ConnectionState.CONNECTABLE

    if has_following:
        logger.info("Following button visible, but connection state is ambiguous")

    return ConnectionState.UNKNOWN


def detect_connection_state(page: Page) -> ConnectionState:
    try:
        profile_slug = _current_profile_slug(page)
        scope = _get_primary_profile_scope(page)
        if scope:
            state = resolve_connection_state(
                has_pending=_has_visible_pending_button(scope, profile_slug),
                has_connected_marker=_has_connected_marker(scope),
                has_connect=_has_visible_connect_button(scope, profile_slug),
                has_following=_has_visible_following_button(scope),
            )
            if state == ConnectionState.PENDING:
                logger.info("Detected PENDING state - connection already sent")
            elif state == ConnectionState.CONNECTED:
                logger.info("Detected CONNECTED state - 1st degree connection")
            elif state == ConnectionState.CONNECTABLE:
                logger.info("Detected CONNECTABLE state - Connect control visible")
            return state

        if _has_visible_pending_button(page, profile_slug, allow_text_only=False):
            logger.info("Detected PENDING state - connection already sent")
            return ConnectionState.PENDING

        if _has_visible_connect_button(page, profile_slug, allow_text_only=False):
            logger.info("Detected CONNECTABLE state - Connect control visible")
            return ConnectionState.CONNECTABLE

        if _has_visible_following_button(page):
            logger.info("Following button visible, but connection state is ambiguous")

        return ConnectionState.UNKNOWN

    except Exception as e:
        logger.warning(f"State detection failed: {e}")
        return ConnectionState.UNKNOWN
