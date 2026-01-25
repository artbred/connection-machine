import logging
from dataclasses import dataclass, field
from typing import Optional
from playwright.sync_api import Page, Locator

from human_actions import HumanActions

logger = logging.getLogger(__name__)

MAIN_PROFILE_CONTAINER = "div.scaffold-layout__main"

CONNECT_IN_DROPDOWN_PATTERNS = [
    "div[role='menu'] button:has-text('Connect')",
    "div.artdeco-dropdown__content button:has-text('Connect')",
    "[class*='dropdown'] button:has-text('Connect')",
]

MORE_BUTTON_PATTERNS = [
    "button[aria-label*='More actions']",
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
                selector=selector,
                expected_text=button_text,
                success_count=1
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


def _is_valid_connect_button(locator: Locator) -> bool:
    try:
        if not locator.is_visible(timeout=500):
            return False
        
        box = locator.bounding_box(timeout=500)
        if not box or box["width"] == 0 or box["height"] == 0:
            return False
        
        if locator.is_disabled(timeout=300):
            return False
        
        text = locator.inner_text(timeout=300).strip()
        return text == "Connect"
    except Exception:
        return False


def _get_main_container(page: Page) -> Locator:
    container = page.locator(MAIN_PROFILE_CONTAINER)
    if container.count() > 0:
        return container.first
    return page.locator("main").first


def _find_direct_connect_button(page: Page) -> Optional[Locator]:
    container = _get_main_container(page)
    buttons = container.locator("button").filter(has_text="Connect")
    for i in range(min(buttons.count(), 10)):
        try:
            btn = buttons.nth(i)
            if _is_valid_connect_button(btn):
                return btn
        except Exception:
            continue
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
    
    container = _get_main_container(page)
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


def get_cached_selector(page: Page, page_variant: str, expected_text: str) -> Optional[str]:
    cached = selector_cache.get(page_variant, expected_text)
    if not cached:
        return None
    
    try:
        locator = page.locator(cached).first
        if not locator.is_visible(timeout=500):
            return None
        
        actual_text = locator.inner_text(timeout=300).strip()
        if actual_text != expected_text:
            selector_cache.record_failure(page_variant, expected_text)
            logger.debug(f"Cache miss: expected '{expected_text}', found '{actual_text}'")
            return None
        
        selector_cache.record_success(page_variant, expected_text)
        logger.info(f"Cache hit: {cached}")
        return cached
        
    except Exception as e:
        logger.debug(f"Cache lookup failed: {e}")
        return None


def save_selector_to_cache(page_variant: str, button_text: str, selector: str) -> None:
    selector_cache.put(page_variant, button_text, selector)
