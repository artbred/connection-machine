import os
import logging
import httpx
from dotenv import load_dotenv

load_dotenv()

NOTIFICATIONS_URL = os.getenv("TELEGRAM_NOTIFICATIONS_URL")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("TELEGRAM_API_KEY")

logger = logging.getLogger(__name__)

def send_notification(message: str):
    if not NOTIFICATIONS_URL or not CHAT_ID or not API_KEY:
        return

    url = f"{NOTIFICATIONS_URL}"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    data = {
        "chat_id": CHAT_ID,
        "messages": [message],
        "disable_notification": False,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        response = httpx.post(url, headers=headers, json=data, timeout=30)
        response.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")
