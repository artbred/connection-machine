import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import Page, Locator

from human_actions import HumanActions

logger = logging.getLogger(__name__)

CONNECT_WORD_RE = re.compile(r"\bconnect\b")

PROFILE_ACTION_CONTAINER_SELECTORS = [
    "div.pvs-profile-actions",
    "section.pv-top-card",
    "main section:has(h1)",
    "div.ph5:has(button[aria-label*='More'], button[aria-label*='Connect'], button:has-text('Connect'), a[href*='/preload/custom-invite/'])",
]

DIRECT_CONNECT_PATTERNS = [
    "a[href*='/preload/custom-invite/']",
    "a[aria-label*='connect' i]",
    "a:has-text('Connect')",
    "button:has-text('Connect')",
    "button[aria-label*='Connect' i]",
    "[role='button']:has-text('Connect')",
    "[role='button'][aria-label*='Connect' i]",
]

CONNECT_IN_DROPDOWN_PATTERNS = [
    "div[role='menu'] button:has-text('Connect')",
    "div[role='menu'] button[aria-label*='Connect' i]",
    "div[role='menu'] [role='button']:has-text('Connect')",
    "div[role='menu'] [role='button'][aria-label*='Connect' i]",
    "div.artdeco-dropdown__content button:has-text('Connect')",
    "div.artdeco-dropdown__content button[aria-label*='Connect' i]",
    "div.artdeco-dropdown__content [role='button']:has-text('Connect')",
    "div.artdeco-dropdown__content [role='button'][aria-label*='Connect' i]",
    "[class*='dropdown'] button:has-text('Connect')",
    "[class*='dropdown'] button[aria-label*='Connect' i]",
    "[class*='dropdown'] [role='button']:has-text('Connect')",
    "[class*='dropdown'] [role='button'][aria-label*='Connect' i]",
]

MORE_BUTTON_PATTERNS = [
    "button[aria-label*='More actions']",
    "button[aria-label*='More']",
    "button:has-text('More')",
]


@dataclass
class CacheEntry:
    selector: str
    expected_text: str
    success_count: int = 0
    failure_count: int = 0


@dataclass
class SelectorCache:
    entries: dict[str, CacheEntry] = field(default_factory=dict)

    def get(self, page_variant: str, expected_text: str) -> Optional[str]:
        key = f"{page_variant}:{expected_text}"
        entry = self.entries.get(key)
        if entry:
            return entry.selector
        return None

    def put(self, page_variant: str, button_text: str, selector: str) -> None:
        key = f"{page_variant}:{button_text}"
        existing = self.entries.get(key)
        if existing:
            existing.selector = selector
            existing.success_count += 1
        else:
            self.entries[key] = CacheEntry(
                selector=selector, expected_text=button_text, success_count=1
            )
        logger.info(f"Cached selector: {selector} for '{button_text}'")

    def record_failure(self, page_variant: str, expected_text: str) -> None:
        key = f"{page_variant}:{expected_text}"
        entry = self.entries.get(key)
        if entry:
            entry.failure_count += 1

    def record_success(self, page_variant: str, expected_text: str) -> None:
        key = f"{page_variant}:{expected_text}"
        entry = self.entries.get(key)
        if entry:
            entry.success_count += 1


selector_cache = SelectorCache()


def _locator_accessible_text(locator: Any, timeout: int = 300) -> str:
    parts = []
    try:
        text = (locator.inner_text(timeout=timeout) or "").strip()
        if text:
            parts.append(text)
    except Exception:
        pass

    try:
        aria_label = (locator.get_attribute("aria-label") or "").strip()
        if aria_label:
            parts.append(aria_label)
    except Exception:
        pass

    return " ".join(parts)


def _locator_matches_expected_text(locator: Any, expected_text: str) -> bool:
    expected = (expected_text or "").strip().lower()
    if not expected:
        return True

    actual = _locator_accessible_text(locator).lower()
    return bool(actual and (expected in actual or actual in expected))


def _is_valid_connect_button(locator: Any) -> bool:
    try:
        if not locator.is_visible(timeout=500):
            return False

        box = locator.bounding_box(timeout=500)
        if not box or box["width"] == 0 or box["height"] == 0:
            return False

        if locator.is_disabled(timeout=300):
            return False

        href = (locator.get_attribute("href") or "").lower()
        text = _locator_accessible_text(locator).lower()
        if "disconnect" in text or "disconnect" in href:
            return False
        return (
            "/preload/custom-invite/" in href
            or CONNECT_WORD_RE.search(text) is not None
        )
    except Exception:
        return False


def _current_profile_slug(page: Page) -> str:
    parsed = urlparse(page.url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "in":
        return parts[1].lower()
    return ""


def _targets_current_profile(locator: Any, profile_slug: str) -> bool:
    if not profile_slug:
        return False

    href = (locator.get_attribute("href") or "").lower()
    if not href:
        return False

    parsed = urlparse(href)
    query = {key.lower(): value for key, value in parse_qs(parsed.query).items()}
    vanity_names = [value.lower() for value in query.get("vanityname", [])]
    return profile_slug in vanity_names or f"/in/{profile_slug}" in parsed.path.lower()


def _get_profile_action_container(page: Page) -> Optional[Locator]:
    for selector in PROFILE_ACTION_CONTAINER_SELECTORS:
        locator = page.locator(selector)
        for index in range(min(locator.count(), 5)):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible(timeout=500):
                    return candidate
            except Exception:
                continue

    return None


def _find_direct_connect_button(page: Page) -> Optional[Locator]:
    container = _get_profile_action_container(page)
    profile_slug = _current_profile_slug(page)
    scopes: list[Any] = []
    if container:
        scopes.append(container)
    scopes.append(page)

    valid_buttons: list[tuple[Any, Locator]] = []

    for scope in scopes:
        buttons = scope.locator(", ".join(DIRECT_CONNECT_PATTERNS))
        for i in range(min(buttons.count(), 10)):
            try:
                btn = buttons.nth(i)
                if not _is_valid_connect_button(btn):
                    continue
                if _targets_current_profile(btn, profile_slug):
                    return btn
                valid_buttons.append((scope, btn))
            except Exception:
                continue

    if profile_slug:
        if container:
            for scope, btn in valid_buttons:
                if scope == container:
                    return btn
        return None

    if valid_buttons:
        return valid_buttons[0][1]
    return None


def _find_connect_in_dropdown(page: Page) -> Optional[Locator]:
    for pattern in CONNECT_IN_DROPDOWN_PATTERNS:
        try:
            locator = page.locator(pattern).first
            if _is_valid_connect_button(locator):
                return locator
        except Exception:
            continue
    return None


def try_heuristic_connect(page: Page, human: HumanActions) -> bool:
    direct_btn = _find_direct_connect_button(page)
    if direct_btn:
        try:
            direct_btn.scroll_into_view_if_needed()
            human.random_sleep(0.3, 0.6)
            direct_btn.click(delay=100)
            human.random_sleep(0.5, 1.0)
            logger.info("Clicked Connect via direct button heuristic")
            return True
        except Exception as e:
            logger.debug(f"Direct button click failed: {e}")

    dropdown_btn = _find_connect_in_dropdown(page)
    if dropdown_btn:
        try:
            dropdown_btn.scroll_into_view_if_needed()
            human.random_sleep(0.2, 0.4)
            dropdown_btn.click(delay=100)
            human.random_sleep(0.5, 1.0)
            logger.info("Clicked Connect in open dropdown via heuristic")
            return True
        except Exception as e:
            logger.debug(f"Dropdown button click failed: {e}")

    container = _get_profile_action_container(page)
    if not container:
        return False

    for more_pattern in MORE_BUTTON_PATTERNS:
        try:
            more_btn = container.locator(more_pattern).first
            if not more_btn.is_visible(timeout=500):
                continue

            more_btn.scroll_into_view_if_needed()
            human.random_sleep(0.2, 0.4)
            more_btn.click(delay=100)
            human.random_sleep(0.5, 1.0)

            dropdown_connect = _find_connect_in_dropdown(page)
            if dropdown_connect:
                dropdown_connect.click(delay=100)
                human.random_sleep(0.5, 1.0)
                logger.info("Clicked Connect via More dropdown heuristic")
                return True

            page.keyboard.press("Escape")
            human.random_sleep(0.2, 0.4)

        except Exception:
            continue

    return False


def get_cached_selector(
    page: Page, page_variant: str, expected_text: str
) -> Optional[str]:
    cached = selector_cache.get(page_variant, expected_text)
    if not cached:
        return None

    try:
        locator = page.locator(cached).first
        if not locator.is_visible(timeout=500):
            return None

        actual_text = _locator_accessible_text(locator)
        if not _locator_matches_expected_text(locator, expected_text):
            selector_cache.record_failure(page_variant, expected_text)
            logger.debug(
                f"Cache miss: expected '{expected_text}', found '{actual_text}'"
            )
            return None

        selector_cache.record_success(page_variant, expected_text)
        logger.info(f"Cache hit: {cached}")
        return cached

    except Exception as e:
        logger.debug(f"Cache lookup failed: {e}")
        return None


def save_selector_to_cache(page_variant: str, button_text: str, selector: str) -> None:
    selector_cache.put(page_variant, button_text, selector)
