import json
import logging
import random

from datetime import datetime, timedelta
from db import SessionLocal, Task, TaskType, TaskStatus
from tasks.invite import InviteTask
from tasks.post import PostTask

logger = logging.getLogger(__name__)


class SessionExpiredException(Exception):
    """Raised when the LinkedIn session has expired."""

    pass


class TaskDispatcher:
    def __init__(self, page):
        self.page = page
        self.handlers = {
            TaskType.SEND_INVITE: InviteTask(page),
            TaskType.CREATE_POST: PostTask(page),
        }
        self.rate_limits = {
            TaskType.SEND_INVITE: 15,
            TaskType.CREATE_POST: 50,
        }
        self.next_execution_at: dict[TaskType, datetime] = {}
        self._previously_blocked: set[TaskType] = set()
        self._last_idle_log: datetime | None = None
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
                # Log idle status at most once per 5 minutes
                now = datetime.utcnow()
                should_log = (
                    self._last_idle_log is None
                    or (now - self._last_idle_log) > timedelta(minutes=5)
                )
                if should_log and (pending_types or blocked_types):
                    if blocked_types:
                        logger.info(f"Waiting: {len(blocked_types)} task type(s) rate-limited")
                    elif not pending_types:
                        logger.info("Idle: no pending tasks")
                    self._last_idle_log = now
                return

            # Reset idle log when we have work to do
            self._last_idle_log = None
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

            except SessionExpiredException as e:
                logger.warning(f"Session expired during task {task_to_run.id}: {e}")
                task_to_run.status = TaskStatus.PENDING
                raise e

            except Exception as e:
                logger.error(f"Task failed: {e}")
                task_to_run.status = TaskStatus.FAILED
                task_to_run.error = str(e)
                task_to_run.executed_at = datetime.utcnow()

            finally:
                db.commit()
