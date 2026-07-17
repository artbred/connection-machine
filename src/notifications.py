import html
import logging
import os
import re

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


def escape_html_text(value: str) -> str:
    return html.escape(value or "", quote=False)


def _strip_html(value: str) -> str:
    return html.unescape(HTML_TAG_PATTERN.sub("", value or ""))


def _telegram_config() -> tuple[str | None, str | None, str | None]:
    """Read Telegram config at call time.

    Capturing these at import made it impossible to disable notifications once
    the module was loaded — a test run with real creds in .env would send live
    messages. Reading per call lets tests (and any runtime change) blank them.
    """
    return (
        os.getenv("TELEGRAM_NOTIFICATIONS_URL"),
        os.getenv("TELEGRAM_CHAT_ID"),
        os.getenv("TELEGRAM_API_KEY"),
    )


def _post_notification(url: str, api_key: str, payload: dict) -> None:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    response = httpx.post(url, headers=headers, json=payload, timeout=30)
    try:
        response.raise_for_status()
    except Exception as exc:
        body_preview = (response.text or "")[:300]
        raise RuntimeError(f"{exc}; response={body_preview}") from exc


def send_notification(message: str, *, parse_mode: str = "HTML") -> bool:
    url, chat_id, api_key = _telegram_config()
    if not url or not chat_id or not api_key:
        return False

    payload = {
        "chat_id": chat_id,
        "messages": [message],
        "disable_notification": False,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        _post_notification(url, api_key, payload)
        return True
    except Exception as exc:
        if parse_mode != "HTML":
            logger.error("Failed to send notification: %s", exc)
            return False

        logger.warning("HTML notification failed, retrying without parse mode: %s", exc)
        fallback_payload = {
            "chat_id": chat_id,
            "messages": [_strip_html(message)],
            "disable_notification": False,
            "disable_web_page_preview": True,
        }
        try:
            _post_notification(url, api_key, fallback_payload)
            return True
        except Exception as fallback_exc:
            logger.error("Failed to send notification: %s", fallback_exc)
            return False
