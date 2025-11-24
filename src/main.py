import json
import logging
import os
import socket
import subprocess
import time
import urllib.request
from contextlib import contextmanager

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from db import init_db
from dispatcher import TaskDispatcher
from exceptions import SessionExpiredException

# --- Configuration ---
INTERNAL_DEBUG_PORT = 9224
SOCKS_HOST = os.getenv("SOCKS_HOST")  # e.g., proxy.example.com
SOCKS_PORT = os.getenv("SOCKS_PORT", "1080")
SOCKS_USER = os.getenv("SOCKS_USER")
SOCKS_PASS = os.getenv("SOCKS_PASS")

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


@contextmanager
def proxy_bridge():
    """
    Starts a local pproxy instance to bridge auth if credentials are provided.
    Returns the string address (e.g., 'socks5://127.0.0.1:12345') or None.
    """
    # 1. If no proxy host is set, return None (Direct Connection)
    if not SOCKS_HOST:
        yield None
        return

    # 2. Calculate upstream URL
    # Format: socks5://user:pass@host:port
    if SOCKS_USER and SOCKS_PASS:
        upstream = f"socks5://{SOCKS_USER}:{SOCKS_PASS}@{SOCKS_HOST}:{SOCKS_PORT}"
    else:
        upstream = f"socks5://{SOCKS_HOST}:{SOCKS_PORT}"

    # 3. Find a local port and start pproxy
    local_port = get_free_port()

    # pproxy command: listen on local_port, relay to upstream
    cmd = [
        "pproxy",
        "-l",
        f"socks5://127.0.0.1:{local_port}",
        "-r",
        upstream,
        "-v",  # verbose (optional)
    ]

    print(
        f"[Proxy] Starting bridge: 127.0.0.1:{local_port} -> {SOCKS_HOST}:{SOCKS_PORT}"
    )

    # Start pproxy in the background
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Give it a moment to bind
    time.sleep(0.5)

    try:
        yield f"socks5://127.0.0.1:{local_port}"
    finally:
        print("[Proxy] Shutting down bridge...")
        proc.terminate()
        proc.wait()


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
        with urllib.request.urlopen(
            f"http://127.0.0.1:{INTERNAL_DEBUG_PORT}/json/version"
        ) as response:
            data = json.loads(response.read().decode())
            logger.info(f"DevTools listening on {data['webSocketDebuggerUrl']}")
    except Exception as e:
        logger.error(f"Failed to get DevTools URL: {e}")


def main():
    init_db()

    try:
        with proxy_bridge() as local_proxy_url:
            launch_args = [
                f"--remote-debugging-port={INTERNAL_DEBUG_PORT}",
                "--remote-debugging-address=127.0.0.1",
                "--remote-allow-origins=*",
                "--no-sandbox",
            ]

            launch_args.append("--proxy-server=socks5://127.0.0.1:10808")

            # if local_proxy_url:
            #     launch_args.append("--proxy-server=socks5://127.0.0.1:10808")

            with sync_playwright() as p:
                browser = p.chromium.launch_persistent_context(
                    user_data_dir="./data/trel-chrome",
                    headless=False,
                    args=launch_args,
                )

                log_ws_endpoint()

                page = browser.new_page()
                check_ip(page)

                is_logged_in = False
                max_login_attempts = 5

                for _ in range(max_login_attempts):
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
                            max_login_attempts -= 1
                            if max_login_attempts == 0:
                                raise Exception("Failed to re-authenticate to LinkedIn")
                        else:
                            max_login_attempts = 5
                            logger.info("Re-authentication successful.")

                    time.sleep(10)

                browser.close()

    except Exception as e:
        logger.error(f"An error occurred: {e}")


if __name__ == "__main__":
    main()
