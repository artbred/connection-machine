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
        self._init_spacing_from_db()

    def _init_spacing_from_db(self):
        """Initialize next execution times from last executed tasks in DB."""
        with SessionLocal() as db:
            for task_type in self.rate_limits.keys():
                last_task = (
                    db.query(Task)
                    .filter(
                        Task.type == task_type,
                        Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
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

    def check_rate_limit(self, task_type: TaskType, logged_rate_limits: set = None) -> bool:
        """Check if the rate limit for the given task type has been reached."""
        limit = self.rate_limits.get(task_type)
        if not limit:
            return True

        last_24h = datetime.utcnow() - timedelta(hours=24)
        with SessionLocal() as db:
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
                if logged_rate_limits is None or task_type not in logged_rate_limits:
                    logger.warning(
                        f"Rate limit reached for {task_type}: {count}/{limit} in last 24h"
                    )
                    if logged_rate_limits is not None:
                        logged_rate_limits.add(task_type)
                return False

            next_allowed = self.next_execution_at.get(task_type)
            if next_allowed and datetime.utcnow() < next_allowed:
                if logged_rate_limits is None or task_type not in logged_rate_limits:
                    wait_time = (next_allowed - datetime.utcnow()).seconds // 60
                    logger.info(
                        f"Spacing delay: waiting ~{wait_time} min before next {task_type}"
                    )
                    if logged_rate_limits is not None:
                        logged_rate_limits.add(task_type)
                return False

        return True

    def schedule_next_execution(self, task_type: TaskType):
        """Set the next allowed execution time for a task type after completion."""
        interval = self.get_spacing_interval(task_type)
        self.next_execution_at[task_type] = datetime.utcnow() + interval
        logger.info(f"Next {task_type} scheduled in ~{interval.seconds // 60} minutes")

    def poll(self):
        """Fetch and execute pending tasks."""
        with SessionLocal() as db:
            tasks = (
                db.query(Task)
                .filter(Task.status == TaskStatus.PENDING)
                .order_by(Task.created_at)
                .limit(10)
                .all()
            )

            if not tasks:
                return

            task_to_run = None
            logged_rate_limits = set()
            for task in tasks:
                if self.check_rate_limit(task.type, logged_rate_limits):
                    task_to_run = task
                    break

            if not task_to_run:
                logger.info("All pending tasks are currently rate limited.")
                return

            logger.info(f"Found task: {task_to_run}")

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
                self.schedule_next_execution(task_to_run.type)

            finally:
                db.commit()
