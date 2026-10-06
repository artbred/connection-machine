import logging
import re
from enum import Enum

from patchright.sync_api import Error as PatchrightError
from playwright.sync_api import Error as PlaywrightError, Page

from connect_heuristics import (
    ACTION_SELECTOR,
    _control_identity_matches,
    _normalize,
    is_target_action,
)
from linkedin_profile import (
    ProfileIdentity,
    assert_profile_identity,
    get_profile_topcard,
    is_profile_owned_element,
)

logger = logging.getLogger(__name__)


class ConnectionState(str, Enum):
    CONNECTABLE = "connectable"
    PENDING = "pending"
    CONNECTED = "connected"
    UNKNOWN = "unknown"


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


def detect_connection_state(page: Page, identity: ProfileIdentity) -> ConnectionState:
    """Read only the verified owner's controls and degree badge; never sidebars."""
    assert_profile_identity(page, identity)
    scope = get_profile_topcard(page, identity)
    if scope is None:
        return ConnectionState.UNKNOWN
    has_pending = has_connect = has_following = has_connected_marker = False
    controls = scope.locator(ACTION_SELECTOR)
    for index in range(controls.count()):
        control = controls.nth(index)
        try:
            if not control.is_visible() or not is_profile_owned_element(
                control, page, identity
            ):
                continue
            if not _control_identity_matches(control, identity):
                continue
            text = _normalize(control.inner_text(timeout=500))
            aria = _normalize(control.get_attribute("aria-label") or "")
            if text in {"pending", "withdraw", "withdraw invitation"} or re.match(
                r"^(?:pending\b|withdraw invitation\b)", aria
            ):
                has_pending = True
            elif is_target_action(control, page, identity):
                has_connect = True
            elif text == "following" or aria == "following":
                has_following = True
        except (PlaywrightError, PatchrightError):
            continue
    badges = scope.locator(
        "[class*='distance-badge'], [class*='dist-value'], "
        "[aria-label='1st degree connection'], [aria-label='1st degree']"
    )
    for index in range(badges.count()):
        badge = badges.nth(index)
        try:
            if not badge.is_visible() or not is_profile_owned_element(
                badge, page, identity
            ):
                continue
            text = _normalize(badge.inner_text(timeout=500)).lstrip("· ")
            aria = _normalize(badge.get_attribute("aria-label") or "")
            if text in {"1st", "1st degree", "1st degree connection"} or aria in {
                "1st degree",
                "1st degree connection",
            }:
                has_connected_marker = True
                break
        except (PlaywrightError, PatchrightError):
            continue
    # Identity drift during reads must not become an apparently benign UNKNOWN.
    assert_profile_identity(page, identity)
    return resolve_connection_state(
        has_pending, has_connected_marker, has_connect, has_following
    )
