import json
import logging
import random

from datetime import datetime, timedelta
from db import SessionLocal, Task, TaskType, TaskStatus
from tasks.invite import InviteTask
from tasks.comment import FeedCommentTask
from tasks.post import PostTask
from exceptions import SessionExpiredException, TaskSkippedException

logger = logging.getLogger(__name__)

SKIP_COOLDOWNS: dict[tuple[TaskType, str], timedelta] = {
    (TaskType.SEND_INVITE, "weekly_limit_reached"): timedelta(hours=12),
    (TaskType.SEND_INVITE, "withdrawal_cooldown"): timedelta(hours=6),
}


class TaskDispatcher:
    def __init__(self, page):
        self.page = page
        self.handlers = {
            TaskType.SEND_INVITE: InviteTask(page),
            TaskType.CREATE_POST: PostTask(page),
            TaskType.COMMENT_FEED_POST: FeedCommentTask(page),
        }
        self.rate_limits = {
            TaskType.SEND_INVITE: 10,
            TaskType.CREATE_POST: 50,
            TaskType.COMMENT_FEED_POST: 12,
        }
        self.next_execution_at: dict[TaskType, datetime] = {}
        self._previously_blocked: set[TaskType] = set()
        self._last_idle_log: datetime | None = None
        self._logged_no_pending: bool = False
        self._init_spacing_from_db()

    def _init_spacing_from_db(self):
        """Initialize next execution times from last executed tasks in DB."""
        self.next_execution_at.clear()
        with SessionLocal() as db:
            for task_type in self.rate_limits.keys():
                last_task = (
                    db.query(Task)
                    .filter(
                        Task.type == task_type,
                        Task.status == TaskStatus.COMPLETED,
                    )
                    .order_by(Task.executed_at.desc())
                    .first()
                )
                if last_task and last_task.executed_at:
                    interval = self.get_spacing_interval(task_type)
                    next_allowed = last_task.executed_at + interval
                    if next_allowed > datetime.utcnow():
                        self.next_execution_at[task_type] = next_allowed
                        wait_min = (next_allowed - datetime.utcnow()).seconds // 60
                        logger.info(
                            f"Restored spacing for {task_type}: ~{wait_min} min remaining"
                        )

                cooldown_until = self._get_restored_cooldown(db, task_type)
                if cooldown_until:
                    restored_until = self.next_execution_at.get(task_type)
                    if not restored_until or cooldown_until > restored_until:
                        self.next_execution_at[task_type] = cooldown_until
                        wait_min = (cooldown_until - datetime.utcnow()).seconds // 60
                        logger.info(
                            f"Restored cooldown for {task_type}: ~{wait_min} min remaining"
                        )

    def _get_restored_cooldown(
        self,
        db,
        task_type: TaskType,
    ) -> datetime | None:
        latest_cooldown_end: datetime | None = None

        for (cooldown_task_type, reason), cooldown in SKIP_COOLDOWNS.items():
            if cooldown_task_type != task_type:
                continue

            last_skipped = (
                db.query(Task)
                .filter(
                    Task.type == task_type,
                    Task.status == TaskStatus.FAILED,
                    Task.error == reason,
                    Task.executed_at.isnot(None),
                )
                .order_by(Task.executed_at.desc())
                .first()
            )
            if not last_skipped or not last_skipped.executed_at:
                continue

            cooldown_end = last_skipped.executed_at + cooldown
            if cooldown_end <= datetime.utcnow():
                continue

            if not latest_cooldown_end or cooldown_end > latest_cooldown_end:
                latest_cooldown_end = cooldown_end

        return latest_cooldown_end

    def get_spacing_interval(self, task_type: TaskType) -> timedelta:
        """Calculate randomized spacing between tasks based on rate limit."""
        limit = self.rate_limits.get(task_type, 100)
        base_seconds = (24 * 60 * 60) / limit  # seconds per task
        randomized = base_seconds * random.uniform(0.7, 1.3)
        return timedelta(seconds=randomized)

    def cleanup_zombie_tasks(self):
        """Reset tasks that were stuck in PROCESSING state (e.g. due to crash)."""
        with SessionLocal() as db:
            zombies = db.query(Task).filter(Task.status == TaskStatus.PROCESSING).all()
            if zombies:
                logger.warning(
                    f"Found {len(zombies)} zombie tasks. Resetting to PENDING."
                )
                for task in zombies:
                    task.status = TaskStatus.PENDING
                db.commit()

    def cleanup_old_pending_posts(self):
        """Delete pending CREATE_POST tasks older than 1 hour."""
        with SessionLocal() as db:
            one_hour_ago = datetime.utcnow() - timedelta(hours=1)
            old_posts = (
                db.query(Task)
                .filter(
                    Task.type == TaskType.CREATE_POST,
                    Task.status == TaskStatus.PENDING,
                    Task.created_at < one_hour_ago,
                )
                .all()
            )
            if old_posts:
                logger.warning(
                    f"Deleting {len(old_posts)} stale pending CREATE_POST tasks (older than 1 hour)."
                )
                for task in old_posts:
                    db.delete(task)
                db.commit()

    def schedule_next_execution(self, task_type: TaskType):
        """Set the next allowed execution time for a task type after completion."""
        interval = self.get_spacing_interval(task_type)
        self.next_execution_at[task_type] = datetime.utcnow() + interval
        logger.info(f"Next {task_type} scheduled in ~{interval.seconds // 60} minutes")

    def schedule_skip_cooldown(self, task_type: TaskType, reason: str):
        """Apply a temporary task-type cooldown for skip reasons that indicate platform limits."""
        cooldown = SKIP_COOLDOWNS.get((task_type, reason))
        if not cooldown:
            return

        next_allowed = datetime.utcnow() + cooldown
        existing = self.next_execution_at.get(task_type)
        if existing and existing > next_allowed:
            return

        self.next_execution_at[task_type] = next_allowed
        logger.warning(
            f"{task_type} cooling down for {cooldown} due to skip reason: {reason}"
        )

    def get_rate_limited_types(self, pending_types: set[TaskType]) -> list[TaskType]:
        """Return list of task types currently blocked by rate limits or spacing delays.

        Only checks task types that are in pending_types (have pending tasks).
        Only logs when blocking state changes to reduce noise.
        """
        if not pending_types:
            return []

        blocked = []
        last_24h = datetime.utcnow() - timedelta(hours=24)

        with SessionLocal() as db:
            for task_type, limit in self.rate_limits.items():
                if task_type not in pending_types:
                    continue

                # Check daily rate limit
                count = (
                    db.query(Task)
                    .filter(
                        Task.type == task_type,
                        Task.executed_at >= last_24h,
                        Task.status == TaskStatus.COMPLETED,
                    )
                    .count()
                )
                if count >= limit:
                    blocked.append(task_type)
                    continue

                # Check spacing delay
                next_allowed = self.next_execution_at.get(task_type)
                if next_allowed and datetime.utcnow() < next_allowed:
                    blocked.append(task_type)

        # Log only on state changes
        blocked_set = set(blocked)
        newly_blocked = blocked_set - self._previously_blocked
        newly_unblocked = self._previously_blocked - blocked_set

        for task_type in newly_blocked:
            next_allowed = self.next_execution_at.get(task_type)
            if next_allowed:
                wait_min = (next_allowed - datetime.utcnow()).seconds // 60
                logger.info(f"{task_type} blocked (next in ~{wait_min} min)")
            else:
                logger.info(f"{task_type} blocked (rate limit reached)")

        for task_type in newly_unblocked:
            logger.info(f"{task_type} unblocked, ready to execute")

        self._previously_blocked = blocked_set
        return blocked

    def poll(self):
        """Fetch and execute pending tasks."""
        # Get distinct pending task types first
        with SessionLocal() as db:
            pending_types = set(
                row[0]
                for row in db.query(Task.type)
                .filter(Task.status == TaskStatus.PENDING)
                .distinct()
                .all()
            )

        blocked_types = self.get_rate_limited_types(pending_types)

        with SessionLocal() as db:
            query = db.query(Task).filter(Task.status == TaskStatus.PENDING)
            if blocked_types:
                query = query.filter(Task.type.notin_(blocked_types))

            task_to_run = query.order_by(Task.created_at).first()

            if not task_to_run:
                now = datetime.utcnow()

                # Log "no pending tasks" once when queue is empty
                if not pending_types and not self._logged_no_pending:
                    logger.info("Idle: no pending tasks")
                    self._logged_no_pending = True

                # Log rate-limited status periodically (every 5 minutes)
                if blocked_types:
                    should_log = (
                        self._last_idle_log is None
                        or (now - self._last_idle_log) > timedelta(minutes=5)
                    )
                    if should_log:
                        logger.info(f"Waiting: {len(blocked_types)} task type(s) rate-limited")
                        self._last_idle_log = now

                return

            # Reset flags when we have work to do
            self._last_idle_log = None
            self._logged_no_pending = False
            logger.info(f"Executing: {task_to_run}")

            task_to_run.status = TaskStatus.PROCESSING
            db.commit()

            try:
                handler = self.handlers.get(task_to_run.type)
                if not handler:
                    raise ValueError(f"No handler for task type: {task_to_run.type}")

                payload = json.loads(task_to_run.payload)
                handler.run(payload)
                task_to_run.status = TaskStatus.COMPLETED
                task_to_run.executed_at = datetime.utcnow()
                self.schedule_next_execution(task_to_run.type)

            except TaskSkippedException as e:
                logger.info(f"Task {task_to_run.id} skipped: {e.reason}")
                task_to_run.status = TaskStatus.FAILED
                task_to_run.error = e.reason
                task_to_run.executed_at = datetime.utcnow()
                self.schedule_skip_cooldown(task_to_run.type, e.reason)
                # Most skips do not count toward rate limits; platform-limit skips may set a cooldown

            except SessionExpiredException as e:
                logger.warning(f"Session expired during task {task_to_run.id}: {e}")
                task_to_run.status = TaskStatus.PENDING
                raise e

            except Exception as e:
                error_str = str(e).lower()
                # Check if this might be a session-related error
                session_indicators = [
                    "login",
                    "sign in",
                    "session",
                    "unauthorized",
                    "authentication",
                    "net::err_aborted",  # Often happens on auth redirects
                ]
                is_session_error = any(indicator in error_str for indicator in session_indicators)

                # Also check page state if we can
                if not is_session_error:
                    try:
                        current_url = self.page.url
                        if "/login" in current_url or "/checkpoint" in current_url:
                            is_session_error = True
                        elif self.page.locator("form.login__form").count() > 0:
                            is_session_error = True
                    except Exception:
                        pass  # Page might be in bad state, continue with original error

                if is_session_error:
                    logger.warning(f"Detected session issue during task {task_to_run.id}: {e}")
                    task_to_run.status = TaskStatus.PENDING
                    db.commit()
                    raise SessionExpiredException(f"Session issue detected: {e}")

                logger.error(f"Task failed: {e}")
                task_to_run.status = TaskStatus.FAILED
                task_to_run.error = str(e)
                task_to_run.executed_at = datetime.utcnow()

            finally:
                db.commit()
