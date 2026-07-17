import base64
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import urlparse

from playwright.sync_api import Locator

from .base import BaseTask
from invite_state import InviteStateStore, INVITE_SKIP_COOLDOWNS
from llm import generate_connection_message, get_next_connect_action
from notifications import escape_html_text, send_notification
from connection_state import detect_connection_state, ConnectionState
from connect_heuristics import (
    try_heuristic_connect,
    get_cached_selector,
    save_selector_to_cache,
)
from exceptions import TaskSkippedException

logger = logging.getLogger(__name__)

MAX_CONNECT_ITERATIONS = 5
INVITE_HISTORY_RETENTION_DAYS = 30
WEEKLY_LIMIT_CONFIRMATION_WINDOW = timedelta(minutes=15)
MAX_INVITE_MESSAGE_LENGTH = 200
MAX_PROFILE_CONTENT_LENGTH = 15000
MIN_PROFILE_CONTENT_LENGTH = 200
PROFILE_HYDRATION_TIMEOUT_SECONDS = 8.0

# LinkedIn renders these recommendation/ad modules inside <main> on profile
# pages. They are full of OTHER people's names and headlines and must never
# reach the message-generation LLM.
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

# Extracts only the prospect's own content from the profile page. LinkedIn's
# SDUI serves structurally different renders per load (hashed class names,
# varying section nesting, sometimes no <h1>), so selection is by tag/role and
# heading text only. Subtrees are hidden, main.innerText is read (innerText
# honors display:none and skips collapsed junk), then styles are restored —
# all synchronous, so the page never observably changes.
PROFILE_CONTENT_EXTRACTION_JS = """
(blockedHeadings) => {
  const main = document.querySelector('main');
  if (!main) return null;
  const isBlocked = (text) => {
    const t = (text || '').trim().toLowerCase();
    return blockedHeadings.some((h) => t.startsWith(h));
  };
  const nameEl =
    main.querySelector('h1') ||
    main.querySelector('section h2') ||
    main.querySelector('h2');
  const profileName = nameEl
    ? (nameEl.innerText || '').trim().split('\\n')[0].trim()
    : '';
  const hidden = [];
  const hide = (el) => {
    if (!el || hidden.some((entry) => entry[0] === el)) return;
    hidden.push([el, el.style.display]);
    el.style.display = 'none';
  };
  try {
    main
      .querySelectorAll('aside, footer, [role="dialog"], button, form')
      .forEach(hide);
    for (const heading of main.querySelectorAll('h2, h3')) {
      if (isBlocked(heading.innerText)) {
        hide(heading.closest('section') || heading.parentElement);
      }
    }
    for (const section of main.querySelectorAll('section')) {
      const heading = section.querySelector('h2, h3');
      if (!heading || !/^activity/i.test((heading.innerText || '').trim())) {
        continue;
      }
      for (const item of section.querySelectorAll('li')) {
        const text = (item.innerText || '').trim();
        if (!text) continue;
        // Own posts are headed by the bare name; any trailing words mean a
        // verb banner ("NAME commented on this" / "likes this" / "reposted
        // this") whose body is ANOTHER person's post.
        const first = text.split('\\n')[0].trim();
        const rest = first.slice(profileName.length).trim();
        const authored =
          profileName &&
          first.toLowerCase().startsWith(profileName.toLowerCase()) &&
          (rest === '' || /^[·•\\s]*(1st|2nd|3rd\\+?)$/i.test(rest));
        const repost = /reposted this/i.test(text.slice(0, 300));
        if (!authored || repost) hide(item);
      }
    }
    return { name: profileName, content: main.innerText };
  } finally {
    for (const [el, previous] of hidden) {
      el.style.display = previous;
    }
  }
}
"""
ADD_NOTE_SELECTOR = "button[aria-label*='Add a note' i], button:has-text('Add a note')"
SEND_INVITATION_SELECTOR = (
    "button[aria-label*='Send invitation' i], "
    "button:has-text('Send invitation'), "
    "button:has-text('Send without a note'), "
    "div[role='dialog'] button[aria-label='Send' i], "
    "div[role='dialog'] button:has-text('Send')"
)
INVITE_NOTE_SELECTOR = (
    "textarea#custom-message, "
    "textarea[name='message'], "
    "textarea.connect-button-send-invite__custom-message, "
    "[contenteditable='true'][role='textbox'], "
    "[contenteditable='true']"
)
INVITE_HISTORY_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "invite_history.json"
)

PROFILE_CONTAINER_SELECTORS = [
    "div.pvs-profile-actions",
    "section.pv-top-card",
    "main section:has(h1)",
    "div.ph5:has(button[aria-label*='Connect'], button[aria-label*='More'])",
    "main section",
]

DROPDOWN_SELECTOR = "div.artdeco-dropdown__content:visible, div[role='menu']:visible"
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
    "navigation_timeout": "Profile navigation timed out",
    "navigation_error": "Profile navigation failed",
    "send_button_timeout": "Send invitation button timed out",
    "add_note_unreachable": "Could not reach the invite modal",
    "llm_invalid_response": "LLM selector guidance was invalid",
}


def _normalize_feedback_text(text: str) -> str:
    return " ".join(text.lower().split())


def sanitize_profile_content(name: Any, content: Any) -> Tuple[str, str]:
    """Normalize extracted profile data; drop content too thin to personalize.

    Generating a message from a barely-loaded page makes the LLM invent or
    latch onto stray text, so below the minimum we return no content and the
    invite goes out without a note instead.
    """
    clean_name = " ".join(str(name or "").split())
    clean_content = str(content or "").strip()
    if len(clean_content) > MAX_PROFILE_CONTENT_LENGTH:
        clean_content = clean_content[:MAX_PROFILE_CONTENT_LENGTH]
    if len(clean_content) < MIN_PROFILE_CONTENT_LENGTH:
        return clean_name, ""
    return clean_name, clean_content


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
) -> str:
    lines = [f"<b>{escape_html_text(title)}</b>"]
    if profile_url:
        lines.append(f"Profile: {escape_html_text(profile_url)}")
    if state:
        lines.append(f"State: {escape_html_text(state)}")
    if reason:
        lines.append(f"Reason: {escape_html_text(describe_invite_reason(reason))}")
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

    def _wait_for_profile_hydration(self):
        """Nudge lazy modules to load, then wait for main's text to stabilize.

        Some SDUI renders stream sections (About, Experience) seconds after
        domcontentloaded; capturing before that leaves the LLM with little
        besides recommendation modules.
        """
        try:
            for fraction in (0.35, 0.7):
                self.page.evaluate(
                    "fraction => window.scrollTo(0, Math.floor(document.body.scrollHeight * fraction))",
                    fraction,
                )
                self.human.random_sleep(0.8, 1.4)
            self.page.evaluate("() => window.scrollTo(0, 0)")
        except Exception as exc:
            logger.debug("Profile hydration scroll failed: %s", exc)

        deadline = time.monotonic() + PROFILE_HYDRATION_TIMEOUT_SECONDS
        previous_length = -1
        while time.monotonic() < deadline:
            try:
                length = self.page.evaluate(
                    "() => { const main = document.querySelector('main'); return main ? main.innerText.length : 0; }"
                )
            except Exception:
                return
            if length and length == previous_length:
                return
            previous_length = length
            self.human.random_sleep(0.6, 1.0)

    def get_profile_content(self) -> Tuple[str, str]:
        """Return (profile_name, content) with only the prospect's own content.

        Excludes recommendation modules, other authors' reposts, footer and
        dialogs so the generated note can only be grounded in the prospect's
        page. Content is empty when extraction fails or yields too little.
        """
        try:
            self.page.wait_for_selector("main", state="attached", timeout=5000)
            self._wait_for_profile_hydration()

            result = self.page.evaluate(
                PROFILE_CONTENT_EXTRACTION_JS,
                PROFILE_FOREIGN_MODULE_HEADINGS,
            )
            if not result:
                return "", ""

            name, content = sanitize_profile_content(
                result.get("name"), result.get("content")
            )
            if not content:
                logger.warning(
                    "Profile content too thin to personalize the invite note"
                )
            return name, content
        except Exception as e:
            logger.error(f"Error getting profile content: {e}")
            return "", ""

    def _wait_for_add_note(self, timeout: int = 2000) -> bool:
        try:
            self.page.wait_for_selector(ADD_NOTE_SELECTOR, timeout=timeout)
            logger.info("'Add a note' button detected")
            return True
        except Exception:
            return False

    def _wait_for_invite_modal(self, timeout: int = 5000) -> bool:
        try:
            self.page.wait_for_selector(
                f"{ADD_NOTE_SELECTOR}, {SEND_INVITATION_SELECTOR}, {INVITE_NOTE_SELECTOR}",
                timeout=timeout,
            )
            logger.info("Invite modal detected")
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

    def _check_invitation_success(self) -> bool:
        try:
            toast = self.page.locator("div.artdeco-toast-item").first
            toast.wait_for(state="visible", timeout=3000)
            text = toast.inner_text().lower()
            logger.debug(f"Success toast content: {text}")
            return (
                "invitation sent" in text
                or "invite sent" in text
                or "invitation pending" in text
            )
        except Exception:
            return False

    def _is_send_modal_open(self) -> bool:
        try:
            dialog = self.page.locator("div[role='dialog']:visible").first
            if not dialog.is_visible(timeout=500):
                return False
        except Exception:
            return False

        selectors = [INVITE_NOTE_SELECTOR, SEND_INVITATION_SELECTOR, ADD_NOTE_SELECTOR]
        for selector in selectors:
            try:
                if dialog.locator(selector).first.is_visible(timeout=500):
                    return True
            except Exception:
                continue
        return True

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
        try:
            dialog = self.page.locator("div[role='dialog']:visible").first
            if dialog.is_visible(timeout=500):
                buttons = dialog.locator("button")
                fallback = None
                for index in range(min(buttons.count(), 20)):
                    button = buttons.nth(index)
                    label = _locator_accessible_text(button).lower()
                    is_send = (
                        "send invitation" in label
                        or "send without a note" in label
                        or label == "send"
                    )
                    if not is_send:
                        continue
                    if self._is_enabled_button(button):
                        return button
                    if fallback is None:
                        fallback = button

                if fallback is not None:
                    return fallback
        except Exception as exc:
            logger.debug("Could not resolve send button inside invite dialog: %s", exc)

        return self.page.locator(SEND_INVITATION_SELECTOR).first

    def _get_invite_note_editor(self) -> Locator:
        try:
            dialog = self.page.locator("div[role='dialog']:visible").first
            if dialog.is_visible(timeout=500):
                return dialog.locator(INVITE_NOTE_SELECTOR).first
        except Exception:
            pass

        return self.page.locator(INVITE_NOTE_SELECTOR).first

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
        if expected_text not in entered_text:
            return False

        try:
            return self._is_enabled_button(self._get_send_invitation_button())
        except Exception:
            return True

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
            except Exception:
                pass
            self.human.random_sleep(0.3, 0.6)

        logger.warning(
            "Invite note direct fill did not enable send; retrying with native setter"
        )
        self._set_invite_note_with_js(custom_message, message)

        entered_text = " ".join(self._get_invite_note_text(custom_message).split())
        if expected_text not in entered_text or not self._is_enabled_button(
            self._get_send_invitation_button()
        ):
            raise TaskSkippedException("invite_not_confirmed")

    def _normalize_profile_url(self, url: str) -> str:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        return f"{parsed.scheme}://{parsed.netloc}{path}/"

    def _confirm_invitation_sent(self, url: str) -> ConnectionState:
        for _ in range(5):
            state = detect_connection_state(self.page)
            if state in {ConnectionState.PENDING, ConnectionState.CONNECTED}:
                return state
            self.human.random_sleep(0.8, 1.4)

        normalized_url = self._normalize_profile_url(url)
        self.page.goto(normalized_url, timeout=60000, wait_until="domcontentloaded")
        self.page.wait_for_selector("main", timeout=15000)
        self.human.random_sleep(1.0, 2.0)

        return detect_connection_state(self.page)

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

    def _after_connect_click(
        self,
        try_personal_message: bool,
        url: str,
        source: str,
        profile_name: str = "",
        profile_content: str = "",
    ) -> Optional[dict]:
        error = self._check_invitation_error()
        if error:
            logger.warning(f"Invitation blocked after {source} click: {error}")
            raise TaskSkippedException(error)

        if self._wait_for_invite_modal():
            logger.info("Invite modal opened via %s", source)
            return self._complete_connection(
                try_personal_message, url, profile_name, profile_content
            )

        error = self._check_invitation_error()
        if error:
            logger.warning(f"Invitation blocked after {source} click: {error}")
            raise TaskSkippedException(error)

        quick_state = detect_connection_state(self.page)
        if quick_state in {ConnectionState.PENDING, ConnectionState.CONNECTED}:
            return self._record_confirmed_invite(url, quick_state, None)

        try:
            if self.page.locator(DROPDOWN_SELECTOR).first.is_visible(timeout=500):
                logger.info("Dropdown opened after %s click", source)
                return None
        except Exception:
            pass

        final_state = self._confirm_invitation_sent(url)
        if final_state in {ConnectionState.PENDING, ConnectionState.CONNECTED}:
            return self._record_confirmed_invite(url, final_state, None)

        return None

    def _complete_connection(
        self,
        try_personal_message: bool,
        url: str,
        profile_name: str = "",
        profile_content: str = "",
    ) -> dict:
        connection_message: Optional[str] = None

        if try_personal_message:
            if not profile_content:
                # Fallback: content was not captured before the Connect click.
                # The extractor hides open dialogs, so the invite modal cannot
                # leak into the content here.
                profile_name, profile_content = self.get_profile_content()
            if profile_content:
                connection_message = generate_connection_message(
                    profile_content, profile_name
                )
                if connection_message:
                    connection_message = connection_message[:MAX_INVITE_MESSAGE_LENGTH]
                    logger.info(f"Generated connection message: {connection_message}")

        if connection_message:
            custom_message = self._get_invite_note_editor()
            try:
                editor_visible = custom_message.is_visible(timeout=500)
            except Exception:
                editor_visible = False

            if not editor_visible:
                add_note = self.page.locator(ADD_NOTE_SELECTOR).first
                if not add_note.is_visible(timeout=1000):
                    raise TaskSkippedException("add_note_unreachable")
                self.human.click(add_note)

            self.page.wait_for_selector(INVITE_NOTE_SELECTOR, timeout=3000)
            self._enter_connection_message(connection_message)

        send_btn = self._get_send_invitation_button()
        if not send_btn.is_visible(timeout=1000):
            raise TaskSkippedException("invite_not_confirmed")
        if not self._is_enabled_button(send_btn):
            logger.warning(
                "Invite send button is disabled: %s",
                _locator_accessible_text(send_btn),
            )
            raise TaskSkippedException("invite_not_confirmed")

        feedback_before_send = self._collect_visible_feedback_texts()

        # Use a precise click here. Missing the modal action button looks like a silent invite failure.
        try:
            send_btn.click(delay=100, timeout=5000)
        except Exception as exc:
            logger.warning(
                "Direct send click failed, retrying with human click: %s", exc
            )
            self.human.click(send_btn)

        self.human.random_sleep(2.0, 4.0)

        error = self._check_invitation_error(ignored_feedback=feedback_before_send)
        if error:
            logger.warning(f"Invitation failed: {error}")
            try:
                close_btn = self.page.locator("button[aria-label='Dismiss']")
                if close_btn.is_visible(timeout=500):
                    close_btn.click()
            except Exception:
                pass
            raise TaskSkippedException(error)

        success_toast_detected = self._check_invitation_success()
        final_state = self._confirm_invitation_sent(url)
        confirmed_state = final_state
        if final_state not in {ConnectionState.PENDING, ConnectionState.CONNECTED}:
            if success_toast_detected:
                confirmed_state = ConnectionState.PENDING
            else:
                if self._is_send_modal_open():
                    logger.warning(
                        "Invite modal still open after send click; treating invite as unconfirmed"
                    )
                logger.warning(
                    "Invite not confirmed after send. success_toast=%s final_state=%s",
                    success_toast_detected,
                    final_state,
                )
                raise TaskSkippedException("invite_not_confirmed")

        return self._record_confirmed_invite(url, confirmed_state, connection_message)

    def send_connection_request(
        self, url: str, try_personal_message: bool = True
    ) -> dict:
        logger.info(f"Sending connection request to {url}...")

        try:
            self.page.goto(url, timeout=60000, wait_until="domcontentloaded")
            self.human.random_sleep(2.0, 4.0)
            self.page.wait_for_selector("main", timeout=15000)

            state = detect_connection_state(self.page)

            if state == ConnectionState.PENDING:
                logger.info("Connection already pending, skipping")
                raise TaskSkippedException("already_pending")

            if state == ConnectionState.CONNECTED:
                logger.info("Already connected, skipping")
                raise TaskSkippedException("already_connected")

            # Capture the prospect's content before any click: the page is
            # still scrollable (no modal), so lazy sections can hydrate, and
            # the note is guaranteed to be grounded in this profile.
            profile_name = ""
            profile_content = ""
            if try_personal_message:
                profile_name, profile_content = self.get_profile_content()

            if try_heuristic_connect(self.page, self.human):
                logger.info("Clicked Connect via heuristics (no LLM needed)")
                self.human.random_sleep(1.0, 2.0)

                result = self._after_connect_click(
                    try_personal_message,
                    url,
                    "heuristic",
                    profile_name,
                    profile_content,
                )
                if result:
                    return result

            cached_selector = get_cached_selector(self.page, "profile_card", "Connect")
            if cached_selector:
                logger.info(f"Using cached selector: {cached_selector}")
                try:
                    locator = self.page.locator(cached_selector).first
                    locator.scroll_into_view_if_needed()
                    self.human.random_sleep(0.3, 0.5)
                    locator.click(delay=100)
                    self.human.random_sleep(1.0, 2.0)

                    result = self._after_connect_click(
                        try_personal_message,
                        url,
                        "cached selector",
                        profile_name,
                        profile_content,
                    )
                    if result:
                        return result
                except Exception:
                    logger.debug("Cached selector failed")

            logger.info("Falling back to LLM for selector detection")

            previous_feedback = None

            for iteration in range(MAX_CONNECT_ITERATIONS):
                logger.info(f"LLM iteration {iteration + 1}/{MAX_CONNECT_ITERATIONS}")

                screenshot_bytes = self.page.screenshot()
                screenshot_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")

                container, container_name = self._get_action_container()
                container_html = container.inner_html()
                logger.debug(f"Using container: {container_name}")

                result = get_next_connect_action(
                    screenshot_base64, container_html, previous_feedback
                )

                if result is None:
                    raise ValueError("LLM returned invalid response")

                selector = result.get("selector")
                expected_text = result.get("expected_text")
                reason = result.get("reason", "")

                logger.info(
                    f"LLM response: selector={selector}, expected_text={expected_text}, reason={reason}"
                )

                if selector is None:
                    logger.info(f"LLM says skip: {reason}")
                    raise TaskSkippedException(reason, cooldown_eligible=False)

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
                                text = _locator_accessible_text(match)
                                if _locator_matches_expected_text(match, expected_text):
                                    target_locator = match
                                    logger.info(
                                        f"Found matching element with text: {text}"
                                    )
                                    break
                            except Exception:
                                continue

                        if not target_locator:
                            previous_feedback = f"Element '{selector}' with text '{expected_text}' is NOT VISIBLE - it's likely inside a closed dropdown. Click the 'More' button first to open the dropdown."
                            logger.info(
                                f"No visible element with expected text '{expected_text}' found among {len(all_matches)} matches (element may be in closed dropdown)"
                            )
                            continue
                    else:
                        visible_matches = [
                            m for m in all_matches if m.is_visible(timeout=300)
                        ]
                        if not visible_matches:
                            previous_feedback = f"Element '{selector}' is NOT VISIBLE - it may be inside a closed dropdown. Click the 'More' button first."
                            logger.info(
                                f"No visible elements found for selector: {selector}"
                            )
                            continue
                        target_locator = visible_matches[0]

                    button_text = _locator_accessible_text(target_locator)

                    is_dropdown_opener = "more" in button_text.lower()

                    target_locator.scroll_into_view_if_needed()
                    self.human.random_sleep(0.3, 0.5)
                    target_locator.click(delay=100)
                    self.human.random_sleep(1.0, 2.0)

                    if is_dropdown_opener:
                        logger.info(
                            "Opened dropdown via LLM-selected action: %s",
                            button_text or selector,
                        )
                        previous_feedback = None
                        continue

                    if button_text:
                        save_selector_to_cache("profile_card", button_text, selector)
                    previous_feedback = None

                except Exception as e:
                    previous_feedback = (
                        f"Selector '{selector}' failed to click: {str(e)[:100]}"
                    )
                    logger.info(f"Suggested selector not clickable: {e}")
                    continue

                result = self._after_connect_click(
                    try_personal_message,
                    url,
                    "LLM-selected action",
                    profile_name,
                    profile_content,
                )
                if result:
                    return result

            raise ValueError(
                f"Could not reach 'Add a note' after {MAX_CONNECT_ITERATIONS} iterations"
            )

        except Exception as e:
            logger.error(f"Error sending connection request: {e}")
            raise e
