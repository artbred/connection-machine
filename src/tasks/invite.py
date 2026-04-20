import base64
import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import urlparse

from playwright.sync_api import Locator

from .base import BaseTask
from invite_state import InviteStateStore, INVITE_SKIP_COOLDOWNS
from llm import generate_connection_message, get_next_connect_action
from markdownify import markdownify as md
from notifications import escape_html_text, send_notification
from connection_state import detect_connection_state, ConnectionState
from connect_heuristics import try_heuristic_connect, get_cached_selector, save_selector_to_cache
from exceptions import TaskSkippedException

logger = logging.getLogger(__name__)

MAX_CONNECT_ITERATIONS = 5
INVITE_HISTORY_RETENTION_DAYS = 30
ADD_NOTE_SELECTOR = "button[aria-label='Add a note']"
SEND_INVITATION_SELECTOR = "button[aria-label='Send invitation']"
INVITE_HISTORY_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "invite_history.json"
)

PROFILE_CONTAINER_SELECTORS = [
    "div.pvs-profile-actions",
    "section.pv-top-card",
    "div.ph5:has(button[aria-label*='Connect'], button[aria-label*='More'])",
    "div.scaffold-layout__main",
    "main",
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

    if (
        "weekly limit" in normalized
        and any(
            marker in normalized
            for marker in (
                "invitation",
                "connection invitation",
                "connection invitations",
                "connection request",
                "connection requests",
                "connect",
            )
        )
    ):
        return "weekly_limit_reached"

    if "try again next week" in normalized and any(
        marker in normalized
        for marker in ("invitation", "connection", "connect")
    ):
        return "weekly_limit_reached"

    if (
        "invitation limit" in normalized
        and ("reached" in normalized or "weekly" in normalized)
    ):
        return "weekly_limit_reached"

    if "withdrawing" in normalized or "withdraw" in normalized:
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
        or "does not contain a \"connect\" option" in normalized
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
                raise TaskSkippedException(active_cooldown["reason"])

            self.send_connection_request(url, try_personal_message)
        except TaskSkippedException as exc:
            normalized_reason = normalize_invite_skip_reason(exc.reason)
            cooldown_until = None
            outcome = "skipped"

            if (
                active_cooldown
                and active_cooldown.get("reason") == normalized_reason
            ):
                cooldown_until = active_cooldown["active_until"]
                outcome = "blocked"
            else:
                cooldown = INVITE_SKIP_COOLDOWNS.get(normalized_reason)
                if cooldown:
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
            raise TaskSkippedException(normalized_reason)
        except Exception as exc:
            normalized_reason = normalize_invite_skip_reason(str(exc))
            self.invite_state.record_event(
                outcome="failed",
                reason=normalized_reason,
                profile_url=url,
                source="invite_task",
            )
            raise

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

            reason = classify_invitation_feedback(text)
            if reason:
                return reason
        except Exception:
            pass

        try:
            alert = self.page.locator("div[role='alert']:visible").first
            if alert.is_visible(timeout=500):
                text = alert.inner_text().lower()
                logger.debug(f"Alert content: {text}")
                reason = classify_invitation_feedback(text)
                if reason:
                    return reason
        except Exception:
            pass

        try:
            page_text = self.page.locator("body").inner_text(timeout=1500).lower()
            reason = classify_invitation_feedback(page_text)
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
        selectors = [
            "#custom-message",
            SEND_INVITATION_SELECTOR,
            ADD_NOTE_SELECTOR,
        ]
        for selector in selectors:
            try:
                if self.page.locator(selector).first.is_visible(timeout=500):
                    return True
            except Exception:
                continue
        return False

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
    ) -> Optional[dict]:
        error = self._check_invitation_error()
        if error:
            logger.warning(f"Invitation blocked after {source} click: {error}")
            raise TaskSkippedException(error)

        if self._wait_for_add_note():
            logger.info("Invite modal opened via %s", source)
            return self._complete_connection(try_personal_message, url)

        error = self._check_invitation_error()
        if error:
            logger.warning(f"Invitation blocked after {source} click: {error}")
            raise TaskSkippedException(error)

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

        send_btn = self.page.locator(SEND_INVITATION_SELECTOR).first
        if not send_btn.is_visible(timeout=1000):
            raise TaskSkippedException("invite_not_confirmed")
        if send_btn.is_disabled(timeout=1000):
            raise TaskSkippedException("invite_not_confirmed")

        # Use a precise click here. Missing the modal action button looks like a silent invite failure.
        try:
            send_btn.click(delay=100, timeout=5000)
        except Exception as exc:
            logger.warning("Direct send click failed, retrying with human click: %s", exc)
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
            raise TaskSkippedException(error)
        
        success_toast_detected = self._check_invitation_success()
        if self._is_send_modal_open():
            logger.warning("Invite modal still open after send click; treating invite as unconfirmed")
            page_text = self.page.locator("body").inner_text().lower()
            reason = classify_invitation_feedback(page_text)
            if reason:
                raise TaskSkippedException(reason)
            raise TaskSkippedException("invite_not_confirmed")

        final_state = self._confirm_invitation_sent(url)
        if final_state not in {ConnectionState.PENDING, ConnectionState.CONNECTED}:
            logger.warning(
                "Invite not confirmed after send. success_toast=%s final_state=%s",
                success_toast_detected,
                final_state,
            )
            raise TaskSkippedException("invite_not_confirmed")

        logger.info("Connection request confirmed with state: %s", final_state.value)
        self._record_invite_history(
            url,
            final_state.value,
            connection_message,
        )
        self.invite_state.record_event(
            outcome="success",
            reason="",
            profile_url=url,
            message_preview=connection_message or "",
            status=final_state.value,
            source="invite_task",
        )
        send_notification(
            _format_invite_notification(
                "Invite confirmed",
                profile_url=url,
                state=final_state.value,
                message=connection_message or "None",
            )
        )

        return {
            "status": final_state.value,
            "message": connection_message,
        }

    def send_connection_request(self, url: str, try_personal_message: bool = True) -> dict:
        logger.info(f"Sending connection request to {url}...")

        try:
            self.page.goto(url, timeout=60000, wait_until="domcontentloaded")
            self.human.random_sleep(2.0, 4.0)
            self.page.wait_for_selector("h2", timeout=15000)

            state = detect_connection_state(self.page)
            
            if state == ConnectionState.PENDING:
                logger.info("Connection already pending, skipping")
                raise TaskSkippedException("already_pending")
            
            if state == ConnectionState.CONNECTED:
                logger.info("Already connected, skipping")
                raise TaskSkippedException("already_connected")

            if try_heuristic_connect(self.page, self.human):
                logger.info("Clicked Connect via heuristics (no LLM needed)")
                self.human.random_sleep(1.0, 2.0)

                result = self._after_connect_click(
                    try_personal_message,
                    url,
                    "heuristic",
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
                    raise TaskSkippedException(reason)

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

                    save_selector_to_cache("profile_card", button_text, selector)
                    previous_feedback = None

                except Exception as e:
                    previous_feedback = f"Selector '{selector}' failed to click: {str(e)[:100]}"
                    logger.info(f"Suggested selector not clickable: {e}")
                    continue

                result = self._after_connect_click(
                    try_personal_message,
                    url,
                    "LLM-selected action",
                )
                if result:
                    return result

            raise ValueError(f"Could not reach 'Add a note' after {MAX_CONNECT_ITERATIONS} iterations")

        except Exception as e:
            logger.error(f"Error sending connection request: {e}")
            raise e
