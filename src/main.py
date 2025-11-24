import logging
import os
import json
import time
import urllib.request

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from db import init_db
from dispatcher import TaskDispatcher
from exceptions import SessionExpiredException

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


def log_ws_endpoint():
    """Fetch and log the DevTools WebSocket URL."""
    try:
        # Give the browser a moment to ensure the DevTools server is up
        time.sleep(2)
        with urllib.request.urlopen("http://127.0.0.1:9224/json/version") as response:
            data = json.loads(response.read().decode())
            logger.info(f"DevTools listening on {data['webSocketDebuggerUrl']}")
    except Exception as e:
        logger.error(f"Failed to get DevTools URL: {e}")


def main():
    init_db()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch_persistent_context(
                user_data_dir="./data/trel-chrome",
                headless=True,
                args=[
                    "--remote-debugging-port=9224",
                    "--no-sandbox",
                    "--remote-debugging-address=127.0.0.1"
                    "--remote-allow-origins=*",
                ],
            )

            log_ws_endpoint()

            page = browser.new_page()
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
                try:
                    dispatcher.poll()
                except SessionExpiredException:
                    logger.warning("Session expired. Re-authenticating...")
                    login(page)
                    if not check_linkedin_auth(page):
                        logger.error("Re-authentication failed.")
                        time.sleep(60)
                    else:
                        logger.info("Re-authentication successful.")

                time.sleep(10)

            browser.close()

    except Exception as e:
        logger.error(f"An error occurred: {e}")


if __name__ == "__main__":
    main()
