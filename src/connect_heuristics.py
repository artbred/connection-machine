import logging
import re
from urllib.parse import parse_qs, quote, urljoin, urlparse

from patchright.sync_api import Error as PatchrightError
from playwright.sync_api import Error as PlaywrightError, Locator, Page

from exceptions import TaskSkippedException
from human_actions import HumanActions
from linkedin_profile import (
    ProfileIdentity,
    assert_profile_identity,
    canonical_profile_url,
    get_profile_topcard,
    is_profile_owned_element,
)

logger = logging.getLogger(__name__)

ACTION_SELECTOR = "button, a[href], [role='button'], [role='menuitem']"
MENU_SELECTOR = "[role='menu'], .artdeco-dropdown__content, [class*='dropdown-content']"
# Cache hints, never a capability: every candidate is identity-checked on every use.
selector_cache: dict[str, str] = {}


def _normalize(value: str) -> str:
    return " ".join(value.split()).casefold()


def _href_identity(href: str, identity: ProfileIdentity) -> bool | None:
    """None means no target evidence; every explicit destination must agree."""
    if not href or href == "#":
        return None
    try:
        parsed = urlparse(urljoin(identity.url, href))
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme not in {"http", "https"}
            or not (host == "linkedin.com" or host.endswith(".linkedin.com"))
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
        ):
            return False
        target = canonical_profile_url(parsed.geturl())
        query = parse_qs(parsed.query, keep_blank_values=True)
        vanity = [
            value
            for key, values in query.items()
            if key.lower() == "vanityname"
            for value in values
        ]
        if vanity:
            if any(
                canonical_profile_url(
                    f"https://www.linkedin.com/in/{quote(value, safe='')}/"
                )
                != identity.url
                for value in vanity
            ):
                return False
            return (
                parsed.path.rstrip("/") == "/preload/custom-invite"
                or target == identity.url
            )
        return target == identity.url
    except ValueError:
        return False


def _label_identity(label: str, identity: ProfileIdentity) -> bool:
    """Accept known generic controls or an exact named recipient, never substrings."""
    text = _normalize(label)
    if not text:
        return True
    if text in {
        "connect",
        "more",
        "more actions",
        "pending",
        "withdraw",
        "withdraw invitation",
        "following",
        "message",
        "send message",
        "send a message",
    }:
        return True
    patterns = (
        r"connect with (.+)",
        r"invite (.+) to connect",
        r"more actions for (.+)",
        r"pending,? click to withdraw invitation sent to (.+)",
        r"withdraw invitation (?:sent )?to (.+)",
        r"message (.+)",
        r"send (?:a )?message to (.+)",
        r"following (.+)",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, text)
        if match:
            return match.group(1) == _normalize(identity.name)
    return False


def _control_identity_matches(locator: Locator, identity: ProfileIdentity) -> bool:
    # A contrary explicit aria label or href overrides even a verified topcard.
    return _href_identity(
        locator.get_attribute("href") or "", identity
    ) is not False and _label_identity(
        locator.get_attribute("aria-label") or "", identity
    )


def _action_kind(locator: Locator) -> str | None:
    text = _normalize(locator.inner_text(timeout=500))
    aria = _normalize(locator.get_attribute("aria-label") or "")
    if "disconnect" in text or "disconnect" in aria:
        return None
    if text == "connect" or re.fullmatch(
        r"connect(?: with .+)?|invite .+ to connect", aria
    ):
        return "connect"
    if text == "more" or re.fullmatch(r"more(?: actions(?: for .+)?)?", aria):
        return "more"
    return None


def _menu_membership(locator: Locator) -> dict:
    return locator.evaluate(
        """(el, selector) => {
            const visible = node => !!(node.getClientRects().length &&
                getComputedStyle(node).visibility !== 'hidden');
            const menus = [...document.querySelectorAll(selector)].filter(visible);
            const roots = menus.filter(node => !menus.some(other => other !== node && other.contains(node)));
            return {inside: menus.some(node => node.contains(el)),
                    unique: roots.length === 1 && roots[0].contains(el),
                    count: roots.length};
        }""",
        MENU_SELECTOR,
    )


def is_target_action(
    locator: Locator,
    page: Page,
    identity: ProfileIdentity,
    *,
    allow_more: bool = False,
    from_profile_menu: bool = False,
) -> bool:
    """Validate a control; callers must revalidate immediately before clicking it.

    from_profile_menu is a capability supplied only after clicking a validated
    target More control. Merely finding an already-open menu does not grant it.
    """
    assert_profile_identity(page, identity)
    try:
        if locator.count() != 1 or not locator.is_visible() or locator.is_disabled():
            return False
        if not locator.evaluate(
            "(el, selector) => el.matches(selector)", ACTION_SELECTOR
        ):
            return False
        if locator.get_attribute("aria-disabled") == "true":
            return False
        if not _control_identity_matches(locator, identity):
            return False
        kind = _action_kind(locator)
        if kind is None or (kind == "more" and not allow_more):
            return False
        menu = _menu_membership(locator)
        if menu["inside"]:
            return kind == "connect" and from_profile_menu and menu["unique"]
        owned = is_profile_owned_element(locator, page, identity)
        if kind == "more":
            return owned and menu["count"] == 0
        return (
            owned
            or _href_identity(locator.get_attribute("href") or "", identity) is True
        )
    except (PlaywrightError, PatchrightError):
        return False


def _find_action(
    page: Page,
    identity: ProfileIdentity,
    *,
    more: bool = False,
    from_profile_menu: bool = False,
) -> Locator | None:
    assert_profile_identity(page, identity)
    scope = get_profile_topcard(page, identity) if more else page
    if scope is None:
        return None
    controls = scope.locator(ACTION_SELECTOR)
    kind = "more" if more else "connect"
    # Shortlist labels in one browser read. Structural paths avoid repeating
    # the page-wide selector query on every subsequent control property read.
    # Paths remain hints: identity and ownership are revalidated before use.
    selectors = controls.evaluate_all(
        r"""(elements, kind) => {
            const word = kind === 'more' ? /\bmore\b/iu : /\bconnect\b/iu;
            const selectors = [];
            for (const el of elements) {
                if (!word.test(el.innerText || '') &&
                    !word.test(el.getAttribute('aria-label') || '')) continue;
                const parts = [];
                for (let node = el; node; node = node.parentElement) {
                    let index = 1;
                    for (let sibling = node.previousElementSibling; sibling;
                         sibling = sibling.previousElementSibling) {
                        if (sibling.tagName === node.tagName) index++;
                    }
                    parts.unshift(node.tagName.toLowerCase() + ':nth-of-type(' + index + ')');
                }
                selectors.push(parts.join(' > '));
            }
            return selectors;
        }""",
        kind,
    )
    for selector in selectors:
        candidate = page.locator(selector)
        if is_target_action(
            candidate,
            page,
            identity,
            allow_more=more,
            from_profile_menu=from_profile_menu,
        ) and _action_kind(candidate) == kind:
            return candidate
    return None


def _click_action(
    locator: Locator,
    page: Page,
    human: HumanActions,
    identity: ProfileIdentity,
    *,
    more: bool = False,
    from_profile_menu: bool = False,
) -> None:
    locator.scroll_into_view_if_needed()
    human.random_sleep(0.2, 0.4)
    if not is_target_action(
        locator, page, identity, allow_more=more, from_profile_menu=from_profile_menu
    ):
        raise TaskSkippedException("profile_identity_mismatch", cooldown_eligible=False)
    try:
        locator.click(delay=100)
    except (PlaywrightError, PatchrightError) as exc:
        # A timeout can occur after dispatch. Never attempt another Connect.
        raise TaskSkippedException(
            "invite_not_confirmed", cooldown_eligible=False
        ) from exc
    human.random_sleep(0.5, 1.0)


def try_heuristic_connect(
    page: Page, human: HumanActions, identity: ProfileIdentity
) -> bool:
    direct = _find_action(page, identity)
    if direct is not None:
        _click_action(direct, page, human, identity)
        return True
    more = _find_action(page, identity, more=True)
    if more is None:
        return False
    _click_action(more, page, human, identity, more=True)
    dropdown = _find_action(page, identity, from_profile_menu=True)
    if dropdown is not None:
        _click_action(dropdown, page, human, identity, from_profile_menu=True)
        return True
    assert_profile_identity(page, identity)
    page.keyboard.press("Escape")
    return False


def get_cached_connect_button(page: Page, identity: ProfileIdentity) -> Locator | None:
    assert_profile_identity(page, identity)
    selector = selector_cache.get("connect")
    if not selector:
        return None
    try:
        candidates = page.locator(selector)
        for index in range(candidates.count()):
            candidate = candidates.nth(index)
            if is_target_action(candidate, page, identity):
                return candidate
    except (PlaywrightError, PatchrightError) as exc:
        logger.debug("Cached Connect selector is no longer usable: %s", exc)
    return None


def save_selector_to_cache(
    page: Page, identity: ProfileIdentity, selector: str
) -> None:
    assert_profile_identity(page, identity)
    try:
        candidates = page.locator(selector)
        for index in range(candidates.count()):
            if is_target_action(candidates.nth(index), page, identity):
                selector_cache["connect"] = selector
                return
    except (PlaywrightError, PatchrightError) as exc:
        logger.debug("Not caching unusable Connect selector: %s", exc)
