import json
import logging
import os
import signal
import socket
import sys
import threading
import time
import urllib

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

from db import init_db
from dispatcher import TaskDispatcher
from exceptions import SessionExpiredException

# Global shutdown flag
shutdown_event = threading.Event()

# --- Configuration ---
INTERNAL_DEBUG_PORT = 9224
SOCKS_PROXY = os.getenv("SOCKS_PROXY")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

load_dotenv()


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


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


def get_free_port():
    """Finds a free port on localhost to bind the bridge to."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def login(page):
    """Login to LinkedIn."""
    logger.info("Logging in to LinkedIn...")
    page.goto("https://www.linkedin.com/login")
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


def log_ws_endpoint():
    """Fetch and log the DevTools WebSocket URL."""
    try:
        # Give the browser a moment to ensure the DevTools server is up
        time.sleep(2)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{INTERNAL_DEBUG_PORT}/json/version"
        ) as response:
            data = json.loads(response.read().decode())
            logger.info(f"DevTools listening on {data['webSocketDebuggerUrl']}")
    except Exception as e:
        logger.error(f"Failed to get DevTools URL: {e}")


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    signal_name = signal.Signals(signum).name
    logger.info(f"Received {signal_name} signal. Initiating graceful shutdown...")
    shutdown_event.set()


def main():
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    logger.info("Initializing database...")
    init_db()

    try:
        launch_args = [
            f"--remote-debugging-port={INTERNAL_DEBUG_PORT}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            "--no-sandbox",
        ]

        if SOCKS_PROXY and len(SOCKS_PROXY) > 0:
            launch_args.append(f"--proxy-server={SOCKS_PROXY}")

        with sync_playwright() as p:
            logger.info("Launching browser with persistent context...")
            browser = p.chromium.launch_persistent_context(
                user_data_dir="./data/trel-chrome",
                headless=HEADLESS,
                args=launch_args,
            )

            log_ws_endpoint()

            page = browser.new_page()
            stealth_sync(page)
            check_ip(page)

            is_logged_in = False
            max_login_attempts = 5

            for _ in range(max_login_attempts):
                if shutdown_event.is_set():
                    break

                if check_linkedin_auth(page):
                    is_logged_in = True
                    break
                else:
                    login(page)
                    time.sleep(5)

            if shutdown_event.is_set():
                return

            if not is_logged_in:
                raise Exception("Failed to login to LinkedIn")

            dispatcher = TaskDispatcher(page)
            dispatcher.cleanup_zombie_tasks()

            logger.info("Starting task dispatcher loop...")
            while not shutdown_event.is_set():
                try:
                    dispatcher.poll()
                except SessionExpiredException:
                    logger.warning("Session expired. Re-authenticating...")

                    re_authenticated = False
                    for attempt in range(max_login_attempts):
                        if shutdown_event.is_set():
                            break

                        login(page)
                        if check_linkedin_auth(page):
                            logger.info("Re-authentication successful.")
                            re_authenticated = True
                            break
                        else:
                            logger.error(
                                f"Re-authentication attempt {attempt + 1}/5 failed."
                            )
                            time.sleep(60)

                    if not re_authenticated:
                        raise Exception(
                            "Failed to re-authenticate to LinkedIn after 5 attempts"
                        )

                time.sleep(10)

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received.")
    except Exception as e:
        logger.error(f"An error occurred: {e}")
    finally:
        logger.info("Shutdown complete.")
        sys.exit(0)


if __name__ == "__main__":
    main()
