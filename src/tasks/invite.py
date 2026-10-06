import base64
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Tuple

from playwright.sync_api import Locator

from .base import BaseTask
from invite_state import InviteStateStore, INVITE_SKIP_COOLDOWNS
from llm import generate_connection_message, get_next_connect_action
from notifications import escape_html_text, send_notification
from connection_state import detect_connection_state, ConnectionState
from connect_heuristics import (
    MENU_SELECTOR,
    try_heuristic_connect,
    get_cached_connect_button,
    is_target_action,
    save_selector_to_cache,
)
from exceptions import TaskSkippedException
from invite_modal import (
    ADD_NOTE_SELECTOR,
    INVITE_NOTE_SELECTOR,
    SEND_INVITATION_NAME,
    find_invite_dialog,
    require_invite_dialog,
)
from linkedin_profile import (
    ProfileIdentity,
    assert_profile_identity,
    canonical_profile_url,
    get_profile_topcard,
    wait_for_profile,
)

logger = logging.getLogger(__name__)

MAX_CONNECT_ITERATIONS = 5
INVITE_HISTORY_RETENTION_DAYS = 30
WEEKLY_LIMIT_CONFIRMATION_WINDOW = timedelta(minutes=15)
MAX_INVITE_MESSAGE_LENGTH = 200

# Audience thresholds apply only to the verified prospect's top card.
AUDIENCE_FILTER_MIN_FOLLOWERS_ENV = "INVITE_MIN_FOLLOWERS"
AUDIENCE_FILTER_REQUIRE_500_CONNECTIONS_ENV = "INVITE_REQUIRE_500_CONNECTIONS"
AUDIENCE_STATS_TOPCARD_SLICE = 3000
AUDIENCE_STATS_MAX_ATTEMPTS = 4
CONNECTIONS_DISPLAY_CAP = 500

# A stats line consists ONLY of counts + keywords + separators ("3,348
# followers", "20,683 followers \u00b7 500+ connections"). Prose that merely
# mentions counts ("I help founders gain 100K followers") never qualifies,
# so it cannot shadow the real stat.
_STAT_PHRASE_RE = re.compile(
    r"(\d[\d.,]*)\s*([KkMm]?)\s*(\+)?\s*(followers|connections?)\b",
    re.IGNORECASE,
)
_STAT_LINE_LEFTOVER_RE = re.compile(r"[\s\u00b7\u2022|+\-]*")
_BARE_COUNT_LINE_RE = re.compile(r"(\d[\d.,]*)\s*([KkMm]?)\s*(\+)?")
_KEYWORD_LINE_RE = re.compile(r"(followers|connections?)", re.IGNORECASE)

# Boundary markers for the audience-count parser only. Personalization uses
# positively identified owner sections in linkedin_profile, never this denylist.
PROFILE_FOREIGN_MODULE_HEADINGS = [
    "more profiles for you",
    "people also viewed",
    "explore premium profiles",
    "people you may know",
    "you might like",
    "pages for you",
    "more posts",
    "promoted",
    "advertisement",
]

INVITE_HISTORY_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "invite_history.json"
)


INVITE_REASON_DESCRIPTIONS = {
    "weekly_limit_reached": "LinkedIn weekly invitation limit reached",
    "withdrawal_cooldown": "LinkedIn is still withdrawing previous invitations",
    "already_pending": "Connection is already pending",
    "already_connected": "Profile is already a 1st-degree connection",
    "connect_unavailable": "No Connect action is available on the profile",
    "invite_not_confirmed": "Invite send action could not be confirmed",
    "profile_not_found": "Profile page was not found",
    "policy_skip": "Invite was skipped by local policy",
    "memorialized_account": "Profile appears to be memorialized",
    "security_checkpoint": "LinkedIn presented a security checkpoint",
    "profile_unavailable": "Profile page content was unavailable",
    "audience_filter": "Profile does not meet follower/connection filters",
    "navigation_timeout": "Profile navigation timed out",
    "navigation_error": "Profile navigation failed",
    "send_button_timeout": "Send invitation button timed out",
    "add_note_unreachable": "Could not reach the invite modal",
    "llm_invalid_response": "LLM selector guidance was invalid",
    "profile_not_ready": "The target profile did not become identifiable",
    "profile_identity_mismatch": "The loaded profile does not match the intended recipient",
    "modal_recipient_mismatch": "The invitation dialog recipient could not be verified",
    "audience_unavailable": "Required audience counts could not be read",
}


def _normalize_feedback_text(text: str) -> str:
    return " ".join(text.lower().split())


def _env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw.replace(",", "").replace("_", "")))
    except ValueError:
        logger.warning("Invalid integer in %s: %r; treating as 0", name, raw)
        return 0


def get_invite_audience_filter() -> Tuple[int, bool]:
    """Return (min_followers, require_500_connections) from the environment."""
    return (
        _env_int(AUDIENCE_FILTER_MIN_FOLLOWERS_ENV),
        _env_flag(AUDIENCE_FILTER_REQUIRE_500_CONNECTIONS_ENV),
    )


def parse_count_token(digits: str, suffix: str) -> Optional[int]:
    cleaned = re.sub(r"[,\s]", "", digits)
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    multiplier = {"k": 1_000, "m": 1_000_000}.get(suffix.lower(), 1)
    return int(value * multiplier)


def _merge_split_stat_lines(lines: list[str]) -> list[str]:
    """Join a bare-count line with a following keyword line.

    Sparse profiles render "49" and "connections" on separate lines
    (observed live 2026-07-17); rejoining them lets the stats-line check
    see the full phrase without any cross-line number merging.
    """
    merged: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line and _BARE_COUNT_LINE_RE.fullmatch(line):
            lookahead = index + 1
            while lookahead < len(lines) and not lines[lookahead]:
                lookahead += 1
            if lookahead < len(lines) and _KEYWORD_LINE_RE.fullmatch(lines[lookahead]):
                merged.append(f"{line} {lines[lookahead]}")
                index = lookahead + 1
                continue
        merged.append(line)
        index += 1
    return merged


def parse_audience_stats(profile_text: str) -> dict:
    """Parse follower/connection counts from the topcard region of a profile.

    Line-based: a count is accepted only from a line that consists purely of
    stat phrases and separators, so prose counts ("gain 100K followers"),
    lines ending in digits above the stats, and mutual-connection phrases
    never parse. Scanning stops at the first foreign-module heading line, so
    a stranger's or Page's count can never win even on sparse profiles where
    those modules start early. First qualifying line per stat wins (the
    prospect's own stats lead <main>'s text).
    """
    text = (profile_text or "")[:AUDIENCE_STATS_TOPCARD_SLICE]
    blocked_headings = set(PROFILE_FOREIGN_MODULE_HEADINGS)

    followers = None
    connections = None
    connections_capped = False

    lines = [line.strip() for line in text.splitlines()]
    for line in _merge_split_stat_lines(lines):
        if not line:
            continue
        if line.lower() in blocked_headings:
            break

        phrases = list(_STAT_PHRASE_RE.finditer(line))
        if not phrases:
            continue
        leftover = _STAT_PHRASE_RE.sub(" ", line)
        if not _STAT_LINE_LEFTOVER_RE.fullmatch(leftover):
            continue

        for match in phrases:
            value = parse_count_token(match.group(1), match.group(2))
            keyword = match.group(4).lower()
            if keyword.startswith("follower"):
                if followers is None:
                    followers = value
            elif connections is None:
                connections = value
                connections_capped = bool(match.group(3))

        if followers is not None and connections is not None:
            break

    return {
        "followers": followers,
        "connections": connections,
        "connections_capped": connections_capped,
    }


def audience_filter_rejection(
    stats: dict,
    min_followers: int,
    require_500_connections: bool,
) -> Optional[str]:
    """Return a rejection reason, or None when the profile passes the filter.

    Fails closed: when a threshold is configured and the corresponding stat is
    not visible on the profile, the profile is rejected. LinkedIn surfaces
    these numbers on virtually every real profile's topcard, so a missing stat
    usually means a small account (or an unusual render worth skipping).
    """
    if min_followers > 0:
        followers = stats.get("followers")
        if followers is None:
            return "follower count not visible on profile"
        if followers < min_followers:
            return f"{followers} followers < required {min_followers}"

    if require_500_connections:
        connections = stats.get("connections")
        if connections is None:
            return "connection count not visible on profile"
        if connections < CONNECTIONS_DISPLAY_CAP:
            return f"{connections} connections < required {CONNECTIONS_DISPLAY_CAP}+"

    return None


def classify_invitation_feedback(text: str) -> Optional[str]:
    normalized = _normalize_feedback_text(text)
    if not normalized:
        return None

    if normalized in {"weekly_limit_reached", "withdrawal_cooldown"}:
        return normalized

    weekly_limit_markers = (
        "weekly invitation limit",
        "reached the weekly invitation limit",
        "reached your weekly invitation limit",
        "weekly limit for connection invitation",
        "weekly limit for connection invitations",
        "weekly connection limit",
    )
    if any(marker in normalized for marker in weekly_limit_markers):
        return "weekly_limit_reached"

    if "weekly limit" in normalized and any(
        marker in normalized
        for marker in (
            "invitation",
            "connection invitation",
            "connection invitations",
            "connection request",
            "connection requests",
            "connect",
        )
    ):
        return "weekly_limit_reached"

    if "try again next week" in normalized and any(
        marker in normalized for marker in ("invitation", "connection", "connect")
    ):
        return "weekly_limit_reached"

    if "invitation limit" in normalized and (
        "reached" in normalized or "weekly" in normalized
    ):
        return "weekly_limit_reached"

    if "withdrawing" in normalized or "withdraw" in normalized:
        return "withdrawal_cooldown"

    if "error" in normalized or "failed" in normalized:
        return "unknown_error"

    return None


def classify_platform_invitation_feedback(text: str) -> Optional[str]:
    normalized = _normalize_feedback_text(text)
    if not normalized:
        return None

    if normalized in {"weekly_limit_reached", "withdrawal_cooldown"}:
        return normalized

    not_sent_markers = (
        "not sent",
        "wasn't sent",
        "was not sent",
        "couldn't send",
        "could not send",
        "unable to send",
    )
    has_not_sent_marker = any(marker in normalized for marker in not_sent_markers)

    if has_not_sent_marker and (
        "weekly invitation limit" in normalized
        or "weekly limit for connection invitation" in normalized
        or "weekly limit for connection invitations" in normalized
        or "reached your weekly invitation limit" in normalized
    ):
        return "weekly_limit_reached"

    if has_not_sent_marker and (
        "withdrawing" in normalized or "withdraw" in normalized
    ):
        return "withdrawal_cooldown"

    if "error" in normalized or "failed" in normalized:
        return "unknown_error"

    return None


def normalize_invite_skip_reason(reason: str) -> str:
    normalized = _normalize_feedback_text(reason)
    if not normalized:
        return reason

    if normalized in {
        "already_pending",
        "already_connected",
        "connect_unavailable",
        "invite_not_confirmed",
        "llm_invalid_response",
        "add_note_unreachable",
        "send_button_timeout",
        "navigation_timeout",
        "navigation_error",
        "screenshot_timeout",
        "selector_timeout",
        "profile_not_found",
        "policy_skip",
        "memorialized_account",
        "security_checkpoint",
        "profile_unavailable",
        "audience_filter",
        "profile_not_ready",
        "profile_identity_mismatch",
        "modal_recipient_mismatch",
        "audience_unavailable",
        "weekly_limit_reached",
        "withdrawal_cooldown",
    }:
        return normalized

    classified = classify_invitation_feedback(reason)
    if classified in {"weekly_limit_reached", "withdrawal_cooldown"}:
        return classified

    if (
        "already connected" in normalized
        or "already a 1st-degree connection" in normalized
        or "1st-degree connection" in normalized
        or "1st degree connection" in normalized
        or ("primary action is message" in normalized and "connect" in normalized)
    ):
        return "already_connected"

    if (
        "already pending" in normalized
        or "connection pending" in normalized
        or "invitation pending" in normalized
        or "invitation sent" in normalized
        or ("pending" in normalized and "button" in normalized)
    ):
        return "already_pending"

    if (
        "does not contain a connect option" in normalized
        or "does not contain a 'connect' option" in normalized
        or 'does not contain a "connect" option' in normalized
        or "connect option is not present" in normalized
        or ("option is not present" in normalized and "connect" in normalized)
        or "connect is not possible" in normalized
        or "no way to send connection request" in normalized
    ):
        return "connect_unavailable"

    if (
        "404" in normalized
        or "this page doesn’t exist" in normalized
        or "this page doesn't exist" in normalized
        or "profile does not exist" in normalized
        or "page does not exist" in normalized
        or "page indicates it does not exist" in normalized
        or "page indicates that it does not exist" in normalized
    ):
        return "profile_not_found"

    if "slavic" in normalized:
        return "policy_skip"

    if "memorialized" in normalized or "in remembrance" in normalized:
        return "memorialized_account"

    if (
        "cloudflare" in normalized
        or "captcha" in normalized
        or "security verification" in normalized
    ):
        return "security_checkpoint"

    if (
        "something went wrong" in normalized
        or "skeleton-loading" in normalized
        or "skeleton loading" in normalized
    ):
        return "profile_unavailable"

    if normalized == "llm returned invalid response":
        return "llm_invalid_response"

    if "could not reach" in normalized and "add a note" in normalized:
        return "add_note_unreachable"

    if (
        normalized.startswith("locator.click: timeout")
        and "send invitation" in normalized
    ):
        return "send_button_timeout"

    if normalized.startswith("page.goto: timeout"):
        return "navigation_timeout"

    if normalized.startswith("page.goto: net::"):
        return "navigation_error"

    if normalized.startswith("page.screenshot: timeout"):
        return "screenshot_timeout"

    if normalized.startswith("page.wait_for_selector: timeout"):
        return "selector_timeout"

    return reason


def describe_invite_reason(reason: str) -> str:
    return INVITE_REASON_DESCRIPTIONS.get(reason, reason.replace("_", " "))


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


def _format_invite_notification(
    title: str,
    *,
    profile_url: str = "",
    state: str = "",
    reason: str = "",
    message: str = "",
    cooldown_until: datetime | None = None,
    audience_stats: dict | None = None,
) -> str:
    lines = [f"<b>{escape_html_text(title)}</b>"]
    if profile_url:
        lines.append(f"Profile: {escape_html_text(profile_url)}")
    if state:
        lines.append(f"State: {escape_html_text(state)}")
    if reason:
        lines.append(f"Reason: {escape_html_text(describe_invite_reason(reason))}")
    if audience_stats:
        followers = audience_stats.get("followers")
        if followers is not None:
            lines.append(f"Followers: {escape_html_text(f'{followers:,}')}")
        connections = audience_stats.get("connections")
        if connections is not None:
            capped = "+" if audience_stats.get("connections_capped") else ""
            lines.append(f"Connections: {escape_html_text(f'{connections:,}{capped}')}")
    if cooldown_until:
        lines.append(
            f"Resume after: {escape_html_text(cooldown_until.strftime('%Y-%m-%d %H:%M UTC'))}"
        )
    if message:
        lines.append(f"Message: {escape_html_text(message)}")
    return "\n".join(lines)


class InviteTask(BaseTask):
    def __init__(self, page):
        super().__init__(page)
        self.invite_state = InviteStateStore()
        self._target_identity: ProfileIdentity | None = None
        self._profile_menu_open = False
        self._send_attempted = False

    def run(self, payload: dict):
        url = payload.get("url")
        if not url:
            raise ValueError("URL is required for invite task")

        self.validate_session()

        try_personal_message = payload.get("try_personal_message", True)
        active_cooldown = self.invite_state.get_active_cooldown()

        try:
            if active_cooldown:
                logger.warning(
                    "Invite cooldown active for %s until %s",
                    active_cooldown.get("reason") or "unknown",
                    active_cooldown["active_until"].isoformat(),
                )
                raise TaskSkippedException(
                    active_cooldown["reason"],
                    cooldown_until=active_cooldown["active_until"],
                    from_active_cooldown=True,
                )

            self.send_connection_request(url, try_personal_message)
        except TaskSkippedException as exc:
            normalized_reason = normalize_invite_skip_reason(exc.reason)
            cooldown_until = None
            outcome = "skipped"
            reason_came_from_canonical_feedback = (
                exc.cooldown_eligible and exc.reason == normalized_reason
            )
            cooldown_eligible = (
                exc.from_active_cooldown or reason_came_from_canonical_feedback
            )

            if exc.from_active_cooldown:
                cooldown_until = exc.cooldown_until
                outcome = "blocked"
            elif active_cooldown and active_cooldown.get("reason") == normalized_reason:
                cooldown_until = active_cooldown["active_until"]
                outcome = "blocked"
            else:
                cooldown = INVITE_SKIP_COOLDOWNS.get(normalized_reason)
                if (
                    normalized_reason == "weekly_limit_reached"
                    and cooldown
                    and not self._has_recent_weekly_limit_signal(url)
                ):
                    logger.warning(
                        "Weekly invite limit feedback seen once for %s; recording without global cooldown until confirmed",
                        url,
                    )
                    cooldown = None

                if cooldown and cooldown_eligible:
                    cooldown_until = datetime.utcnow() + cooldown
                    self.invite_state.set_cooldown(
                        normalized_reason,
                        cooldown_until,
                        source="invite_task",
                        profile_url=url,
                    )

            self.invite_state.record_event(
                outcome=outcome,
                reason=normalized_reason,
                profile_url=url,
                source="invite_task",
                cooldown_until=cooldown_until,
            )
            if cooldown_until:
                send_notification(
                    _format_invite_notification(
                        "Invite blocked",
                        profile_url=url,
                        reason=normalized_reason,
                        cooldown_until=cooldown_until,
                    )
                )
            raise TaskSkippedException(
                normalized_reason,
                cooldown_until=cooldown_until,
                from_active_cooldown=exc.from_active_cooldown,
                cooldown_eligible=cooldown_until is not None,
                retryable_preflight=exc.retryable_preflight,
            )
        except Exception as exc:
            normalized_reason = normalize_invite_skip_reason(str(exc))
            self.invite_state.record_event(
                outcome="failed",
                reason=normalized_reason,
                profile_url=url,
                source="invite_task",
            )
            raise

    def _has_recent_weekly_limit_signal(self, profile_url: str) -> bool:
        cutoff = datetime.utcnow() - WEEKLY_LIMIT_CONFIRMATION_WINDOW
        try:
            events = self.invite_state.get_recent_events(limit=25)
        except Exception:
            return False

        for event in events:
            if event.get("reason") != "weekly_limit_reached":
                continue
            if event.get("outcome") not in {"skipped", "blocked"}:
                continue

            recorded_at = event.get("recorded_at")
            if not isinstance(recorded_at, datetime) or recorded_at < cutoff:
                continue

            if event.get("profile_url") == profile_url:
                continue

            return True

        return False

    def _enforce_audience_filter(self, url: str) -> Optional[dict]:
        """Skip the invite when the profile fails the configured audience filter.

        Runs at visit time on every invite — a queued DB task for a person who
        turns out not to qualify is skipped all the same. Returns the parsed
        stats when the filter is enabled so notifications can include them.
        """
        min_followers, require_500_connections = get_invite_audience_filter()
        if min_followers <= 0 and not require_500_connections:
            return None

        identity = self._require_target_identity()
        stats = {"followers": None, "connections": None, "connections_capped": False}
        for attempt in range(AUDIENCE_STATS_MAX_ATTEMPTS):
            snapshot = assert_profile_identity(self.page, identity)
            stats = parse_audience_stats(snapshot.audience_text)
            has_needed_stats = (
                min_followers <= 0 or stats["followers"] is not None
            ) and (not require_500_connections or stats["connections"] is not None)
            if has_needed_stats:
                break
            if attempt + 1 < AUDIENCE_STATS_MAX_ATTEMPTS:
                self.human.random_sleep(0.7, 1.1)

        if not has_needed_stats:
            logger.info("Required audience counts unavailable for %s", url)
            raise TaskSkippedException(
                "audience_unavailable",
                cooldown_eligible=False,
                retryable_preflight=True,
            )
        rejection = audience_filter_rejection(
            stats, min_followers, require_500_connections
        )
        if rejection:
            logger.info("Audience filter rejected %s: %s", url, rejection)
            raise TaskSkippedException("audience_filter", cooldown_eligible=False)
        logger.info(
            "Audience filter passed for %s (followers=%s, connections=%s%s)",
            url,
            stats["followers"],
            stats["connections"],
            "+" if stats["connections_capped"] else "",
        )
        return stats

    def _read_preflight_profile(self, url: str, *, personalize: bool):
        try:
            return wait_for_profile(self.page, url, personalize=personalize)
        except TaskSkippedException as exc:
            if exc.reason == "profile_not_ready":
                raise TaskSkippedException(
                    exc.reason, cooldown_eligible=False, retryable_preflight=True
                ) from exc
            raise

    def _require_target_identity(self) -> ProfileIdentity:
        identity = self._target_identity
        if identity is None:
            raise TaskSkippedException(
                "profile_identity_mismatch", cooldown_eligible=False
            )
        return identity

    def _wait_for_invite_modal(self, timeout: int = 5000) -> bool:
        identity = self._require_target_identity()
        deadline = time.monotonic() + timeout / 1000
        while True:
            if find_invite_dialog(self.page, identity) is not None:
                return True
            if time.monotonic() >= deadline:
                return False
            self.page.wait_for_timeout(100)

    def _get_action_container(self) -> Tuple[Locator, str]:
        identity = self._require_target_identity()
        assert_profile_identity(self.page, identity)
        if self._profile_menu_open:
            roots = []
            for menu in self.page.locator(MENU_SELECTOR).all():
                if menu.is_visible() and menu.evaluate(
                    """(el, selector) => {
                      for (let parent = el.parentElement; parent; parent = parent.parentElement) {
                        if (parent.matches(selector) && parent.getClientRects().length &&
                            getComputedStyle(parent).visibility !== 'hidden') return false;
                      }
                      return true;
                    }""",
                    MENU_SELECTOR,
                ):
                    roots.append(menu)
            if len(roots) == 1:
                return roots[0], "profile dropdown"
            self._profile_menu_open = False
            if roots:
                raise TaskSkippedException(
                    "connect_unavailable", cooldown_eligible=False
                )
        container = get_profile_topcard(self.page, identity)
        if container is None:
            raise TaskSkippedException(
                "profile_identity_mismatch", cooldown_eligible=False
            )
        return container, "verified profile topcard"

    def _collect_visible_feedback_texts(self) -> set[str]:
        texts: set[str] = set()
        selectors = [
            "div.artdeco-toast-item:visible",
            "div[role='alert']:visible",
        ]

        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                for index in range(min(locator.count(), 5)):
                    item = locator.nth(index)
                    if not item.is_visible(timeout=300):
                        continue
                    text = _normalize_feedback_text(item.inner_text(timeout=500))
                    if text:
                        texts.add(text)
            except Exception:
                continue

        return texts

    def _check_invitation_error(
        self,
        ignored_feedback: set[str] | None = None,
    ) -> Optional[str]:
        ignored_feedback = ignored_feedback or set()

        for label, selector, wait_for_first in (
            ("toast", "div.artdeco-toast-item:visible", True),
            ("alert", "div[role='alert']:visible", False),
        ):
            try:
                locator = self.page.locator(selector)
                if wait_for_first:
                    locator.first.wait_for(state="visible", timeout=3000)

                for index in range(min(locator.count(), 5)):
                    item = locator.nth(index)
                    if not item.is_visible(timeout=500):
                        continue

                    text = item.inner_text(timeout=1000).lower()
                    normalized_text = _normalize_feedback_text(text)
                    if normalized_text in ignored_feedback:
                        logger.debug(
                            "Ignoring pre-existing %s feedback: %s", label, text
                        )
                        continue

                    logger.debug("%s content: %s", label.title(), text)
                    reason = classify_platform_invitation_feedback(text)
                    if reason:
                        return reason
            except Exception:
                pass

        return None

    def _is_enabled_button(self, button: Any) -> bool:
        try:
            if not button.is_visible(timeout=500):
                return False
            if button.is_disabled(timeout=500):
                return False
            aria_disabled = (button.get_attribute("aria-disabled") or "").lower()
            disabled_attr = button.get_attribute("disabled")
            class_name = (button.get_attribute("class") or "").lower()
            return (
                aria_disabled != "true"
                and disabled_attr is None
                and "disabled" not in class_name
            )
        except Exception:
            return False

    def _get_send_invitation_button(self) -> Locator:
        dialog = require_invite_dialog(self.page, self._require_target_identity())
        buttons = dialog.get_by_role("button", name=SEND_INVITATION_NAME)
        visible = [button for button in buttons.all() if button.is_visible()]
        if len(visible) != 1:
            raise TaskSkippedException("invite_not_confirmed", cooldown_eligible=False)
        return visible[0]

    def _get_invite_note_editor(self) -> Locator:
        dialog = require_invite_dialog(self.page, self._require_target_identity())
        editors = dialog.locator(INVITE_NOTE_SELECTOR)
        visible = [editor for editor in editors.all() if editor.is_visible()]
        if len(visible) > 1:
            raise TaskSkippedException("invite_not_confirmed", cooldown_eligible=False)
        return visible[0] if visible else editors.first

    def _get_invite_note_text(self, editor: Any) -> str:
        try:
            tag_name = (editor.evaluate("element => element.tagName") or "").lower()
            if tag_name in {"textarea", "input"}:
                return editor.input_value(timeout=1000)
        except Exception:
            pass

        try:
            return editor.input_value(timeout=1000)
        except Exception:
            pass

        try:
            return editor.inner_text(timeout=1000)
        except Exception:
            return ""

    def _set_invite_note_with_js(self, editor: Locator, message: str) -> None:
        editor.evaluate(
            """
(element, value) => {
  element.focus();

  if (element instanceof HTMLTextAreaElement || element instanceof HTMLInputElement) {
    const setter = Object.getOwnPropertyDescriptor(element.constructor.prototype, 'value')?.set;
    if (setter) {
      setter.call(element, value);
    } else {
      element.value = value;
    }
  } else {
    element.textContent = value;
  }

  element.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
  element.dispatchEvent(new Event('change', {bubbles: true}));
}
""",
            message,
        )

    def _invite_note_is_ready(self, editor: Locator, expected_text: str) -> bool:
        entered_text = " ".join(self._get_invite_note_text(editor).split())
        if expected_text != entered_text:
            return False

        return self._is_enabled_button(self._get_send_invitation_button())

    def _enter_connection_message(self, connection_message: str) -> None:
        message = connection_message[:MAX_INVITE_MESSAGE_LENGTH]
        custom_message = self._get_invite_note_editor()
        expected_text = " ".join(message.split())

        try:
            custom_message.fill("", timeout=1000)
        except Exception:
            pass

        try:
            self.human.type(custom_message, message)
        except Exception:
            logger.warning("Invite note human typing failed; retrying with direct fill")
            try:
                custom_message.fill(message, timeout=3000)
            except Exception:
                self._set_invite_note_with_js(custom_message, message)

        for _ in range(3):
            try:
                if self._invite_note_is_ready(custom_message, expected_text):
                    return
            except TaskSkippedException:
                raise
            except Exception:
                pass
            self.human.random_sleep(0.3, 0.6)

        logger.warning(
            "Invite note typing did not enable send; retrying with direct fill"
        )
        try:
            custom_message.fill(message, timeout=3000)
        except Exception:
            self._set_invite_note_with_js(custom_message, message)

        for _ in range(3):
            try:
                if self._invite_note_is_ready(custom_message, expected_text):
                    return
            except TaskSkippedException:
                raise
            except Exception:
                pass
            self.human.random_sleep(0.3, 0.6)

        logger.warning(
            "Invite note direct fill did not enable send; retrying with native setter"
        )
        self._set_invite_note_with_js(custom_message, message)

        entered_text = " ".join(self._get_invite_note_text(custom_message).split())
        if expected_text != entered_text or not self._is_enabled_button(
            self._get_send_invitation_button()
        ):
            raise TaskSkippedException("invite_not_confirmed")

    def _normalize_profile_url(self, url: str) -> str:
        normalized = canonical_profile_url(url)
        if normalized is None:
            raise ValueError("A LinkedIn /in/ profile URL is required")
        return normalized

    def _confirm_invitation_sent(self, url: str) -> ConnectionState:
        identity = self._require_target_identity()
        if self._normalize_profile_url(url) != identity.url:
            raise TaskSkippedException(
                "profile_identity_mismatch", cooldown_eligible=False
            )
        for _ in range(5):
            assert_profile_identity(self.page, identity)
            # A stale or wrong-recipient dialog must never be used as success
            # evidence even if a different control happens to say Pending.
            find_invite_dialog(self.page, identity)
            state = detect_connection_state(self.page, identity)
            if state in {ConnectionState.PENDING, ConnectionState.CONNECTED}:
                return state
            self.human.random_sleep(0.8, 1.4)

        if find_invite_dialog(self.page, identity) is not None:
            return ConnectionState.UNKNOWN
        self.page.goto(identity.url, timeout=60000, wait_until="domcontentloaded")
        refreshed = wait_for_profile(self.page, identity.url, personalize=False)
        if refreshed.identity != identity:
            raise TaskSkippedException(
                "profile_identity_mismatch", cooldown_eligible=False
            )
        return detect_connection_state(self.page, identity)

    def _record_confirmed_invite(
        self,
        url: str,
        confirmed_state: ConnectionState,
        connection_message: Optional[str],
    ) -> dict:
        logger.info(
            "Connection request confirmed with state: %s", confirmed_state.value
        )
        self._record_invite_history(
            url,
            confirmed_state.value,
            connection_message,
        )
        self.invite_state.record_event(
            outcome="success",
            reason="",
            profile_url=url,
            message_preview=connection_message or "",
            status=confirmed_state.value,
            source="invite_task",
        )
        send_notification(
            _format_invite_notification(
                "Invite confirmed",
                profile_url=url,
                state=confirmed_state.value,
                message=connection_message or "None",
                audience_stats=getattr(self, "_last_audience_stats", None),
            )
        )

        return {
            "status": confirmed_state.value,
            "message": connection_message,
        }

    def _load_invite_history(self) -> dict[str, Any]:
        if not INVITE_HISTORY_PATH.exists():
            return {}

        try:
            raw = json.loads(INVITE_HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read invite history: %s", exc)
            return {}

        if not isinstance(raw, dict):
            return {}

        cutoff = datetime.utcnow() - timedelta(days=INVITE_HISTORY_RETENTION_DAYS)
        pruned: dict[str, Any] = {}
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue

            sent_at = value.get("sent_at")
            if not sent_at:
                continue

            try:
                parsed = datetime.fromisoformat(sent_at)
            except ValueError:
                continue

            if parsed >= cutoff:
                pruned[key] = value

        return pruned

    def get_invite_history_entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for entry_key, value in self._load_invite_history().items():
            if not isinstance(value, dict):
                continue

            sent_at = value.get("sent_at")
            if not sent_at:
                continue

            try:
                sent_at_dt = datetime.fromisoformat(sent_at)
            except ValueError:
                continue

            entries.append(
                {
                    "entry_key": entry_key,
                    "message": str(value.get("message") or ""),
                    "sent_at": sent_at_dt,
                    "status": str(value.get("status") or ""),
                    "url": str(value.get("url") or ""),
                }
            )

        entries.sort(key=lambda entry: entry["sent_at"], reverse=True)
        return entries

    def _record_invite_history(
        self,
        url: str,
        status: str,
        connection_message: Optional[str],
    ):
        history = self._load_invite_history()
        sent_at = datetime.utcnow()
        entry_key = hashlib.sha256(
            f"{self._normalize_profile_url(url)}|{sent_at.isoformat()}".encode("utf-8")
        ).hexdigest()[:16]
        history[entry_key] = {
            "message": connection_message or "",
            "sent_at": sent_at.isoformat(),
            "status": status,
            "url": url,
        }

        INVITE_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        INVITE_HISTORY_PATH.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _after_connect_click(self, url: str, profile_content: str) -> dict:
        try:
            error = self._check_invitation_error()
            if error:
                raise TaskSkippedException(error)
            if self._wait_for_invite_modal():
                return self._complete_connection(url, profile_content)
            final_state = self._confirm_invitation_sent(url)
            if final_state in {ConnectionState.PENDING, ConnectionState.CONNECTED}:
                return self._record_confirmed_invite(url, final_state, None)
            raise TaskSkippedException("invite_not_confirmed", cooldown_eligible=False)
        except TaskSkippedException as exc:
            if exc.reason in {"weekly_limit_reached", "withdrawal_cooldown"}:
                raise
            logger.warning("Post-Connect outcome is unconfirmed (%s)", exc.reason)
            raise TaskSkippedException(
                "invite_not_confirmed", cooldown_eligible=False
            ) from exc
        except Exception as exc:
            logger.warning("Post-Connect confirmation failed: %s", exc)
            raise TaskSkippedException(
                "invite_not_confirmed", cooldown_eligible=False
            ) from exc

    def _complete_connection(self, url: str, profile_content: str) -> dict:
        identity = self._require_target_identity()
        require_invite_dialog(self.page, identity)
        if self._send_attempted:
            raise TaskSkippedException("invite_not_confirmed", cooldown_eligible=False)
        connection_message = None
        if profile_content:
            connection_message = generate_connection_message(
                profile_content, identity.name
            )
            if connection_message:
                connection_message = connection_message[:MAX_INVITE_MESSAGE_LENGTH]

        # Revalidate after the external generation call and after opening the
        # note editor. There is deliberately no post-click content re-scrape.
        dialog = require_invite_dialog(self.page, identity)
        if connection_message:
            if not self._get_invite_note_editor().is_visible():
                add_note = dialog.locator(ADD_NOTE_SELECTOR)
                visible = [button for button in add_note.all() if button.is_visible()]
                if len(visible) != 1:
                    raise TaskSkippedException(
                        "add_note_unreachable", cooldown_eligible=False
                    )
                visible[0].click(timeout=3000)
            self._get_invite_note_editor().wait_for(state="visible", timeout=3000)
            self._enter_connection_message(connection_message)
        else:
            editor = self._get_invite_note_editor()
            if editor.is_visible():
                editor.fill("", timeout=3000)

        send_btn = self._get_send_invitation_button()
        if not self._is_enabled_button(send_btn):
            raise TaskSkippedException("invite_not_confirmed", cooldown_eligible=False)
        editor = self._get_invite_note_editor()
        if editor.is_visible():
            entered = " ".join(self._get_invite_note_text(editor).split())
            expected = " ".join((connection_message or "").split())
            if entered != expected:
                raise TaskSkippedException(
                    "invite_not_confirmed", cooldown_eligible=False
                )

        feedback_before_send = self._collect_visible_feedback_texts()
        require_invite_dialog(self.page, identity)
        self._send_attempted = True
        try:
            send_btn.click(delay=100, timeout=5000)
        except Exception as exc:
            # A timeout can happen after dispatch. Confirm, but never click a
            # second time or regenerate a new note for the same attempt.
            logger.warning(
                "Send click outcome is uncertain; checking target state: %s", exc
            )
        self.human.random_sleep(2.0, 4.0)
        error = self._check_invitation_error(ignored_feedback=feedback_before_send)
        if error:
            raise TaskSkippedException(error)
        final_state = self._confirm_invitation_sent(url)
        if final_state not in {ConnectionState.PENDING, ConnectionState.CONNECTED}:
            raise TaskSkippedException("invite_not_confirmed", cooldown_eligible=False)
        return self._record_confirmed_invite(url, final_state, connection_message)

    def _click_connect_target(self, target: Locator, profile_content: str) -> dict:
        identity = self._require_target_identity()
        target.scroll_into_view_if_needed()
        if not is_target_action(
            target, self.page, identity, from_profile_menu=self._profile_menu_open
        ):
            raise TaskSkippedException(
                "profile_identity_mismatch", cooldown_eligible=False
            )
        try:
            target.click(delay=100, timeout=5000)
        except Exception as exc:
            raise TaskSkippedException(
                "invite_not_confirmed", cooldown_eligible=False
            ) from exc
        return self._after_connect_click(identity.url, profile_content)

    def send_connection_request(
        self, url: str, try_personal_message: bool = True
    ) -> dict:
        url = self._normalize_profile_url(url)
        self._target_identity = None
        self._profile_menu_open = False
        self._send_attempted = False
        self._last_audience_stats = None
        logger.info("Sending connection request to %s", url)
        self.page.goto(url, timeout=60000, wait_until="domcontentloaded")
        self.validate_session()
        snapshot = self._read_preflight_profile(url, personalize=False)
        identity = snapshot.identity
        self._target_identity = identity
        state = detect_connection_state(self.page, identity)
        if state == ConnectionState.PENDING:
            raise TaskSkippedException("already_pending", cooldown_eligible=False)
        if state == ConnectionState.CONNECTED:
            raise TaskSkippedException("already_connected", cooldown_eligible=False)

        self._last_audience_stats = self._enforce_audience_filter(url)
        profile_content = ""
        if try_personal_message:
            snapshot = self._read_preflight_profile(url, personalize=True)
            if snapshot.identity != identity:
                raise TaskSkippedException(
                    "profile_identity_mismatch", cooldown_eligible=False
                )
            profile_content = snapshot.content
            if not profile_content:
                logger.info("Owned profile sections are not ready; omitting the note")
            else:
                logger.info(
                    "Owned profile ready for %s (headline=%d, about=%d, experience=%d characters)",
                    identity.url,
                    len(snapshot.headline),
                    len(snapshot.about),
                    len(snapshot.experience),
                )
        # An invitation dialog left by an earlier task must not be mistaken for the result
        # of a new Connect click, even when its recipient happens to match.
        if find_invite_dialog(self.page, identity) is not None:
            raise TaskSkippedException(
                "modal_recipient_mismatch", cooldown_eligible=False
            )

        if try_heuristic_connect(self.page, self.human, identity):
            return self._after_connect_click(url, profile_content)
        cached = get_cached_connect_button(self.page, identity)
        if cached is not None:
            return self._click_connect_target(cached, profile_content)

        previous_feedback = None
        for iteration in range(MAX_CONNECT_ITERATIONS):
            logger.info(
                "LLM selector iteration %s/%s", iteration + 1, MAX_CONNECT_ITERATIONS
            )
            container, _ = self._get_action_container()
            screenshot = base64.b64encode(self.page.screenshot()).decode("utf-8")
            result = get_next_connect_action(
                screenshot, container.inner_html(), previous_feedback
            )
            assert_profile_identity(self.page, identity)
            if result is None:
                raise TaskSkippedException(
                    "llm_invalid_response", cooldown_eligible=False
                )
            selector = result.get("selector")
            if selector is None:
                raise TaskSkippedException(
                    result.get("reason") or "connect_unavailable",
                    cooldown_eligible=False,
                )
            # Resolve fresh after generation; the old scoped locator may now
            # refer to a different card or a replaced dropdown.
            container, _ = self._get_action_container()
            expected_text = result.get("expected_text") or ""
            try:
                matches = container.locator(selector).all()
            except Exception:
                previous_feedback = (
                    "Invalid selector; choose a visible Connect or More control."
                )
                continue
            candidates = [
                match
                for match in matches
                if _locator_matches_expected_text(match, expected_text)
                and is_target_action(
                    match,
                    self.page,
                    identity,
                    allow_more=True,
                    from_profile_menu=self._profile_menu_open,
                )
            ]
            if len(candidates) != 1:
                previous_feedback = (
                    "Selector must identify one target-owned Connect or More control."
                )
                continue
            target = candidates[0]
            label = _locator_accessible_text(target).lower()
            is_more = bool(re.search(r"\bmore\b", label))
            if not is_more:
                save_selector_to_cache(self.page, identity, selector)
                return self._click_connect_target(target, profile_content)
            target.scroll_into_view_if_needed()
            if not is_target_action(target, self.page, identity, allow_more=True):
                raise TaskSkippedException(
                    "profile_identity_mismatch", cooldown_eligible=False
                )
            try:
                target.click(delay=100, timeout=5000)
            except Exception as exc:
                raise TaskSkippedException(
                    "connect_unavailable", cooldown_eligible=False
                ) from exc
            self._profile_menu_open = True
            self.human.random_sleep(0.5, 1.0)
            previous_feedback = None
        raise TaskSkippedException("connect_unavailable", cooldown_eligible=False)
