import html
import logging
import os
import re

import httpx
from dotenv import load_dotenv

load_dotenv()

NOTIFICATIONS_URL = os.getenv("TELEGRAM_NOTIFICATIONS_URL")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("TELEGRAM_API_KEY")

logger = logging.getLogger(__name__)
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


def escape_html_text(value: str) -> str:
    return html.escape(value or "", quote=False)


def _strip_html(value: str) -> str:
    return html.unescape(HTML_TAG_PATTERN.sub("", value or ""))


def _post_notification(payload: dict) -> None:
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    response = httpx.post(NOTIFICATIONS_URL, headers=headers, json=payload, timeout=30)
    try:
        response.raise_for_status()
    except Exception as exc:
        body_preview = (response.text or "")[:300]
        raise RuntimeError(f"{exc}; response={body_preview}") from exc


def send_notification(message: str, *, parse_mode: str = "HTML") -> bool:
    if not NOTIFICATIONS_URL or not CHAT_ID or not API_KEY:
        return False

    payload = {
        "chat_id": CHAT_ID,
        "messages": [message],
        "disable_notification": False,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        _post_notification(payload)
        return True
    except Exception as exc:
        if parse_mode != "HTML":
            logger.error("Failed to send notification: %s", exc)
            return False

        logger.warning("HTML notification failed, retrying without parse mode: %s", exc)
        fallback_payload = {
            "chat_id": CHAT_ID,
            "messages": [_strip_html(message)],
            "disable_notification": False,
            "disable_web_page_preview": True,
        }
        try:
            _post_notification(fallback_payload)
            return True
        except Exception as fallback_exc:
            logger.error("Failed to send notification: %s", fallback_exc)
            return False
