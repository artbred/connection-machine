import argparse
import json
import logging
import os
import signal
import threading
from contextlib import ExitStack, closing

from datetime import datetime
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()

from dispatcher import TaskDispatcher  # noqa: E402
from db import SessionLocal, Task, TaskStatus, TaskType, init_db  # noqa: E402
from exceptions import SessionExpiredException, TaskSkippedException  # noqa: E402
from metrics import NoopMetrics, create_metrics  # noqa: E402
from tasks.invite import InviteTask, normalize_invite_skip_reason  # noqa: E402
from tasks.comment import FeedCommentTask  # noqa: E402
from tasks.post import PostTask  # noqa: E402
from tasks.notification_reply_invites import (  # noqa: E402
    NOTIFICATION_REPLY_INVITE_SOURCE,
    NotificationReplyInviteScanner,
)
from worker_watchdog import WorkerWatchdog  # noqa: E402

# Global shutdown flag
shutdown_event = threading.Event()

# --- Configuration ---
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
metrics = NoopMetrics()


def check_linkedin_auth(page):
    """Check if the user is logged into LinkedIn."""

    logger.info("Navigating to LinkedIn...")
    try:
        page.goto(
            "https://www.linkedin.com/feed/",
            timeout=30000,
            wait_until="domcontentloaded",
        )

        route = urlsplit(page.url)
        if (route.scheme, route.netloc, route.path.rstrip("/")) != (
            "https", "www.linkedin.com", "/feed"
        ):
            logger.info("Feed redirected away from the authenticated route")
            return False

        # Check for login form
        if page.locator("form.login__form").count() > 0:
            logger.info("Login form detected")
            return False

        # Check for session expired message
        if page.locator("text=session has expired").count() > 0:
            logger.info("Session expired message detected")
            return False

        # Verify we have the authenticated nav/feed
        if page.locator("nav").count() > 0:
            return True

        logger.info("Could not verify authenticated state")
        return False
    except Exception as e:
        logger.error(f"Error checking auth: {e}")
        return False


def _linkedin_login_route(url):
    route = urlsplit(url)
    return (route.scheme, route.netloc) == ("https", "www.linkedin.com") and (
        route.path.rstrip("/") in {"/login", "/uas/login"}
    )


def login(page):
    """Login to LinkedIn."""
    logger.info("Logging in to LinkedIn...")
    if urlsplit(page.url).path.startswith(("/checkpoint", "/challenge")):
        raise RuntimeError("LinkedIn requires account-owner verification")
    try:
        # The feed may already have redirected to login. Do not replace that navigation.
        if not _linkedin_login_route(page.url):
            page.goto("https://www.linkedin.com/login", timeout=60000, wait_until="load")
        username = page.locator('input:is(#username, [autocomplete="username"]):visible')
        password = page.locator('input[type="password"]:is(#password, [autocomplete="current-password"]):visible')
        username.wait_for(state="visible", timeout=30000)
        if not _linkedin_login_route(page.url):
            raise RuntimeError("Not on the first-party login route")
        username.fill(os.environ["LINKEDIN_USERNAME"])
        password.fill(os.environ["LINKEDIN_PASSWORD"])
        page.get_by_role("button", name="Sign in", exact=True).click()

        # Wait for navigation after login (feed or checkpoint)
        try:
            page.wait_for_url(
                lambda url: "/feed" in url
                or "/checkpoint" in url
                or "/login" not in url,
                timeout=30000,
            )
        except Exception:
            # If URL wait times out, give a short delay for page to settle
            shutdown_event.wait(5)
    except Exception as e:
        logger.error("Login failed: %s", type(e).__name__)


def check_ip(page):
    """Check and log the current IP address."""
    logger.info("Checking current IP address...")
    try:
        # Do not send the login tab through a cross-site navigation for diagnostics.
        response = page.request.get("https://api.ipify.org?format=json", timeout=10000)
        try:
            if response.ok:
                logger.info("Current IP: %s", response.json().get("ip"))
            else:
                logger.warning("Failed to get IP address.")
        finally:
            response.dispose()
    except Exception as e:
        logger.error(f"Error checking IP: {e}")


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    signal_name = signal.Signals(signum).name
    logger.info(f"Received {signal_name} signal. Initiating graceful shutdown...")
    shutdown_event.set()


def parse_args():
    parser = argparse.ArgumentParser(description="LinkedIn task automation")
    debug_group = parser.add_mutually_exclusive_group()
    debug_group.add_argument(
        "--dry-run", choices=("post", "comment", "notifications", "invite"),
        help="Prepare an operation without publishing, connecting, notifying or enqueuing",
    )
    parser.add_argument("--profile-url", help="Real target profile for --dry-run invite only")
    debug_group.add_argument(
        "--check-auth",
        action="store_true",
        help="Verify the WebKit login and exit without opening the task queue",
    )
    debug_group.add_argument(
        "--debug-invite",
        metavar="URL",
        help="Debug mode: send invite to a specific profile URL and exit",
    )
    debug_group.add_argument(
        "--debug-feed-comment",
        action="store_true",
        help="Debug mode: generate one feed comment and exit",
    )
    debug_group.add_argument(
        "--debug-notification-reply-invites",
        action="store_true",
        help="Debug mode: scan LinkedIn notifications for reply-based invite candidates and exit",
    )
    parser.add_argument(
        "--no-message",
        action="store_true",
        help="Skip generating personal message (use with --debug-invite or --debug-notification-reply-invites)",
    )
    parser.add_argument(
        "--submit-comment",
        action="store_true",
        help="Actually submit the generated comment when used with --debug-feed-comment",
    )
    parser.add_argument(
        "--send-notification-invite",
        action="store_true",
        help="Actually send the first queued invite when used with --debug-notification-reply-invites",
    )
    args = parser.parse_args()
    if args.dry_run and (args.submit_comment or args.send_notification_invite or args.no_message):
        parser.error("--dry-run cannot be combined with action modifiers")
    if bool(args.profile_url) != (args.dry_run == "invite"):
        parser.error("--profile-url is required for and only allowed with --dry-run invite")
    return args


def _require_member_route(page, *paths):
    url = urlsplit(page.url)
    if (url.scheme, url.netloc) != ("https", "www.linkedin.com") or url.path.rstrip("/") not in paths:
        raise RuntimeError("Dry run did not reach the expected LinkedIn page")
    nav = page.locator(
        "nav a[href^='/mynetwork/']:visible, nav a[href^='/messaging/']:visible, "
        "nav a[href^='https://www.linkedin.com/mynetwork/']:visible, "
        "nav a[href^='https://www.linkedin.com/messaging/']:visible"
    )
    if not nav.count():
        raise RuntimeError("Dry run could not verify signed-in member navigation")


def run_dry_run(page, operation, profile_url=None):
    _require_member_route(page, "/feed")
    paths = ("/feed",)
    if operation == "post":
        PostTask(page).inspect_composer()
        result = "Editable post composer found; draft untouched"
        paths = ("/feed", "/sharing/compose")
    elif operation == "comment":
        candidate = FeedCommentTask(page).prepare_comment({})
        result = f"Comment prepared for post {candidate['post_key']}; not submitted or recorded"
    elif operation == "notifications":
        candidates = NotificationReplyInviteScanner(page).find_reply_candidates(max_candidates=1)
        if not candidates:
            raise TaskSkippedException("no_notification_reply_candidates")
        result = f"Reply candidate {candidates[0]['notification_key']} found; not enqueued or recorded"
        paths = ("/notifications",)
    elif operation == "invite":
        from linkedin_profile import canonical_profile_url

        target = canonical_profile_url(profile_url)
        if not target:
            raise ValueError("A canonical LinkedIn profile URL is required")
        proof = InviteTask(page).preview_connection_request(target)
        result = f"Verified {proof['control_kind']} control and {proof['note_length']}-character note; no click"
        if canonical_profile_url(page.url) != target:
            raise RuntimeError("Dry run left the target profile")
        paths = (urlsplit(page.url).path.rstrip("/"),)
    else:
        raise ValueError(f"Unknown dry run: {operation}")
    _require_member_route(page, *paths)
    logger.info("Dry run: %s.", result)


def get_next_notification_reply_invite_task() -> Task | None:
    with SessionLocal() as db:
        pending_tasks = (
            db.query(Task)
            .filter(
                Task.type == TaskType.SEND_INVITE,
                Task.status == TaskStatus.PENDING,
            )
            .order_by(Task.created_at)
            .yield_per(64)
        )
        for task in pending_tasks:
            try:
                payload = json.loads(task.payload)
            except (TypeError, json.JSONDecodeError):
                continue
            if payload.get("source") == NOTIFICATION_REPLY_INVITE_SOURCE:
                return task
    return None


def run_notification_reply_invite_task(
    page,
    task_id: int,
    *,
    try_personal_message: bool = True,
) -> None:
    with SessionLocal() as db:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            logger.info("Notification reply invite task %s no longer exists", task_id)
            return

        payload = json.loads(task.payload)
        payload["try_personal_message"] = try_personal_message
        task.status = TaskStatus.PROCESSING
        db.commit()

    invite_task = InviteTask(page)
    try:
        invite_task.run(payload)
    except TaskSkippedException as exc:
        with SessionLocal() as db:
            task = db.query(Task).filter(Task.id == task_id).first()
            if task:
                task.status = TaskStatus.FAILED
                task.error = normalize_invite_skip_reason(exc.reason)
                task.executed_at = datetime.utcnow()
                db.commit()
        NotificationReplyInviteScanner(page).mark_task_status(task_id, "failed")
        raise
    except Exception as exc:
        with SessionLocal() as db:
            task = db.query(Task).filter(Task.id == task_id).first()
            if task:
                task.status = TaskStatus.FAILED
                task.error = normalize_invite_skip_reason(str(exc))
                task.executed_at = datetime.utcnow()
                db.commit()
        NotificationReplyInviteScanner(page).mark_task_status(task_id, "failed")
        raise

    with SessionLocal() as db:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task:
            task.status = TaskStatus.COMPLETED
            task.error = None
            task.executed_at = datetime.utcnow()
            db.commit()
    NotificationReplyInviteScanner(page).mark_task_status(task_id, "completed")


def authenticate(page, *, retry_delay=5):
    for attempt in range(6):
        if shutdown_event.is_set():
            raise InterruptedError("Worker is shutting down")
        authenticated = check_linkedin_auth(page)
        metrics.set_linkedin_authenticated(authenticated)
        if authenticated:
            metrics.mark_login_success()
            return
        if attempt < 5:
            metrics.inc_login_attempts()
            login(page)
            shutdown_event.wait(retry_delay)
    raise RuntimeError("Failed to authenticate to LinkedIn after 5 attempts")


class BrowserSession:
    """One WebKit page while busy; no renderer or Playwright driver while idle."""

    def __init__(self, profile_dir):
        self.profile_dir = profile_dir
        self._resources = ExitStack()
        self.page = None
        self._checked_ip = False

    def acquire_page(self, *, existing_session_only=False):
        if self.page is None:
            if existing_session_only and not os.path.isdir(self.profile_dir):
                raise SessionExpiredException("Dry run requires an existing authenticated WebKit profile")
            from playwright.sync_api import sync_playwright

            playwright = self._resources.enter_context(sync_playwright())
            context = playwright.webkit.launch_persistent_context(
                self.profile_dir,
                headless=HEADLESS,
                viewport={"width": 1920, "height": 1080},
            )
            self._resources.callback(context.close)
            page = context.pages[0] if context.pages else context.new_page()
            if existing_session_only:
                if not check_linkedin_auth(page):
                    raise SessionExpiredException("Dry run requires an authenticated WebKit profile")
            else:
                if not self._checked_ip:
                    check_ip(page)
                    self._checked_ip = True
                authenticate(page)
            self.page = page
        return self.page

    def close(self):
        self.page = None
        self._resources.close()


def main():
    global metrics

    args = parse_args()
    metrics = NoopMetrics() if args.dry_run else create_metrics()
    metrics.set_up(True)
    watchdog = WorkerWatchdog()
    watchdog.start()

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        if not (args.check_auth or args.dry_run):
            init_db()
        profile_dir = os.path.join(os.getcwd(), "data", "connection-machine-webkit")
        with closing(BrowserSession(profile_dir)) as browser:
            if args.dry_run:
                run_dry_run(browser.acquire_page(existing_session_only=True), args.dry_run, args.profile_url)
                return
            if args.check_auth:
                browser.acquire_page()
                logger.info("Verified LinkedIn authentication in WebKit.")
                return

            # Debug mode: run single invite task and exit
            if args.debug_invite:
                logger.info(f"Debug mode: sending invite to {args.debug_invite}")
                invite_task = InviteTask(browser.acquire_page())
                invite_task.run(
                    {
                        "url": args.debug_invite,
                        "try_personal_message": not args.no_message,
                    }
                )
                logger.info("Debug invite completed.")
                return

            if args.debug_feed_comment:
                logger.info(
                    "Debug mode: generating one feed comment (%s)",
                    "submit" if args.submit_comment else "dry run",
                )
                comment_task = FeedCommentTask(browser.acquire_page())
                comment_task.run({"dry_run": not args.submit_comment})
                logger.info("Debug feed comment completed.")
                return

            if args.debug_notification_reply_invites:
                logger.info(
                    "Debug mode: scanning notification replies%s",
                    " and sending first queued invite"
                    if args.send_notification_invite
                    else "",
                )
                page = browser.acquire_page()
                scanner = NotificationReplyInviteScanner(page)
                queued = scanner.run(
                    {
                        "max_to_queue": 1 if args.send_notification_invite else 5,
                    }
                )
                snapshot = scanner.get_metrics_snapshot()
                metrics.set_notification_reply_invite_scan(
                    last_scan_timestamp=snapshot["last_scan_timestamp"],
                    latest_engagement_count=snapshot["latest_engagement_count"],
                    seen_count=snapshot["seen_count"],
                    queued_count=snapshot["queued_count"],
                )
                logger.info(
                    "Debug notification reply scan queued %s candidate(s): %s",
                    len(queued),
                    [entry.get("profile_url") for entry in queued],
                )
                if args.send_notification_invite:
                    task = get_next_notification_reply_invite_task()
                    if not task:
                        logger.info("No queued notification reply invite task to send.")
                        return
                    logger.info(
                        "Debug mode: sending notification reply invite task %s", task.id
                    )
                    run_notification_reply_invite_task(
                        page,
                        task.id,
                        try_personal_message=not args.no_message,
                    )
                    logger.info("Debug notification reply invite completed.")
                return

            dispatcher = TaskDispatcher(metrics=metrics, acquire_page=browser.acquire_page)
            logger.info("Task dispatcher initialized.")
            dispatcher.cleanup_zombie_tasks()
            dispatcher.cleanup_db_backed_feed_comment_tasks()
            dispatcher.cleanup_old_pending_posts()

            logger.info("Starting task dispatcher loop...")
            while not shutdown_event.is_set():
                watchdog.heartbeat("dispatcher poll")
                try:
                    dispatcher.poll()
                except SessionExpiredException:
                    logger.warning("Session expired. Re-authenticating...")
                    metrics.set_linkedin_authenticated(False)
                    metrics.inc_reauth()

                    authenticate(browser.acquire_page(), retry_delay=60)
                finally:
                    dispatcher.release_page()
                    browser.close()

                if shutdown_event.wait(10):
                    break

    except (KeyboardInterrupt, InterruptedError):
        logger.info("Worker interrupted.")
        if args.check_auth or args.dry_run:
            raise
    finally:
        metrics.set_up(False)
        metrics.shutdown()
        logger.info("Shutdown complete.")
        watchdog.stop()


if __name__ == "__main__":
    main()
