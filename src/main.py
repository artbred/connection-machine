import httpx
import uuid
import logging
import os
import json
import time


from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from db import init_db
from dispatcher import TaskDispatcher
from exceptions import SessionExpiredException

load_dotenv()


# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

API_BASE_URL = os.getenv(
    "API_BASE_URL", "http://localhost:3000"
)  # Default to localhost for self-hosted
WS_URL = os.getenv(
    "WS_URL", "ws://localhost:3000"
)  # Default to localhost for self-hosted
CONTEXT_FILE = "session_context.json"


def load_local_context():
    """Load session context from a local JSON file."""
    if os.path.exists(CONTEXT_FILE):
        try:
            with open(CONTEXT_FILE, "r") as f:
                logger.info(f"Loading context from {CONTEXT_FILE}")
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load context file: {e}")
    return None


def save_local_context(context):
    """Save session context to a local JSON file."""
    try:
        with open(CONTEXT_FILE, "w") as f:
            json.dump(context, f, indent=2)
        logger.info(f"Context saved to {CONTEXT_FILE}")
    except Exception as e:
        logger.error(f"Failed to save context file: {e}")


def create_session(context=None):
    """Create a new Steel session, optionally injecting context."""
    url = f"{API_BASE_URL}/v1/sessions"
    payload = {
        "sessionId": str(uuid.uuid4()),
        "isSelenium": False,
        "blockAds": False,
        "optimizeBandwidth": False,
        "skipFingerprintInjection": False,
        "deviceConfig": {"device": "desktop"},
    }

    proxy_url = os.getenv("STEEL_PROXY")
    if proxy_url:
        logger.info(f"Using proxy: {proxy_url}")
        payload["use_proxy"] = json.dumps({"server": proxy_url})

    logger.info(f"Payload: {payload}")

    if context:
        logger.info("Injecting existing session context...")
        payload["sessionContext"] = context

    try:
        response = httpx.post(url, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Error creating session: {e}")
        raise


def get_session_context(session_id):
    """Fetch the current context (cookies, local storage) from the running session."""
    url = f"{API_BASE_URL}/v1/sessions/{session_id}/context"
    try:
        response = httpx.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Error fetching session context: {e}")
        return None


def release_session(session_id):
    """Release the Steel session."""
    url = f"{API_BASE_URL}/v1/sessions/{session_id}/release"
    try:
        httpx.post(url, timeout=30)
        logger.info(f"Session {session_id} released.")
    except Exception as e:
        logger.error(f"Error releasing session: {e}")


def check_linkedin_auth(page):
    """Check if the user is logged into LinkedIn."""

    logger.info("Navigating to LinkedIn...")
    try:
        page.goto("https://www.linkedin.com/feed/")

        if page.locator("form.login__form").count() > 0:
            return False

        return True

    except Exception as e:
        logger.error(f"Error checking auth: {e}")
        return False


def login(page):
    """Login to LinkedIn."""
    logger.info("Logging in to LinkedIn...")
    try:
        page.fill("#username", os.getenv("LINKEDIN_USERNAME"))
        page.fill("#password", os.getenv("LINKEDIN_PASSWORD"))
        page.click("button[type='submit']")
        time.sleep(60)
    except Exception as e:
        logger.error(f"Error logging in: {e}")


def check_ip(page):
    """Check and log the current IP address."""
    logger.info("Checking current IP address...")
    try:
        response = page.goto("https://api.ipify.org?format=json")
        if response and response.ok:
            ip_data = json.loads(response.text())
            logger.info(f"Current IP: {ip_data.get('ip')}")
        else:
            logger.warning("Failed to get IP address.")
    except Exception as e:
        logger.error(f"Error checking IP: {e}")


def main():
    init_db()
    session_id = None
    try:
        # 1. Load existing context if available
        context = load_local_context()

        # 2. Create session
        session_data = create_session(context)
        session_id = session_data.get("id")
        if not session_id:
            logger.error("Failed to get session ID.")
            return

        logger.info(f"Session created: {session_id}")

        # 3. Connect Playwright
        cdp_url = f"{WS_URL}?sessionId={session_id}"
        logger.info(f"Connecting to CDP: {cdp_url}")

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_url)
            # Get the default context that was created with our session options
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()

            check_ip(page)

            is_logged_in = False

            for _ in range(5):
                if check_linkedin_auth(page):
                    is_logged_in = True
                    break
                else:
                    login(page)
                    time.sleep(5)

            if not is_logged_in:
                raise Exception("Failed to login to LinkedIn")

            dispatcher = TaskDispatcher(page)
            dispatcher.cleanup_zombie_tasks()
            
            logger.info("Starting task dispatcher loop...")
            while True:
                new_context = get_session_context(session_id)
                if new_context:
                    save_local_context(new_context)
                try:
                    dispatcher.poll()
                except SessionExpiredException:
                    logger.warning("Session expired. Re-authenticating...")
                    login(page)
                    # Verify login
                    if not check_linkedin_auth(page):
                        logger.error("Re-authentication failed.")
                        # Optionally break or sleep longer
                        time.sleep(60)
                    else:
                        logger.info("Re-authentication successful.")

                time.sleep(10)

            browser.close()

    except Exception as e:
        logger.error(f"An error occurred: {e}")
    finally:
        new_context = get_session_context(session_id)
        if new_context:
            save_local_context(new_context)

        if session_id:
            release_session(session_id)


if __name__ == "__main__":
    main()
