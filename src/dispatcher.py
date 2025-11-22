import json
import logging
from datetime import datetime, timedelta
from db import SessionLocal, Task, TaskType, TaskStatus
from tasks.invite import InviteTask
from tasks.post import PostTask

logger = logging.getLogger(__name__)


class TaskDispatcher:
    def __init__(self, page):
        self.page = page
        self.handlers = {
            TaskType.SEND_INVITE: InviteTask(page),
            TaskType.CREATE_POST: PostTask(page),
        }
        self.rate_limits = {
            TaskType.SEND_INVITE: 10,
            TaskType.CREATE_POST: 50,
        }

    def check_rate_limit(self, task_type: TaskType) -> bool:
        """Check if the rate limit for the given task type has been reached."""
        limit = self.rate_limits.get(task_type)
        if not limit:
            return True  # No limit for this task type

        # Count tasks executed in the last 24 hours
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
            logger.warning(
                f"Rate limit reached for {task_type}: {count}/{limit} in last 24h"
            )
            return False

        return True

    def poll(self):
        """Fetch and execute pending tasks."""
        with SessionLocal() as db:
            # Get the next pending task
            task = (
                db.query(Task)
                .filter(Task.status == TaskStatus.PENDING)
                .order_by(Task.created_at)
                .first()
            )

            if not task:
                return

            logger.info(f"Found task: {task}")

            # Check rate limit
            if not self.check_rate_limit(task.type):
                # Skip this task for now
                return

            # Mark as processing
            task.status = TaskStatus.PROCESSING
            db.commit()

            try:
                handler = self.handlers.get(task.type)
                if not handler:
                    raise ValueError(f"No handler for task type: {task.type}")

                payload = json.loads(task.payload)
                handler.run(payload)

                # Mark as completed
                task.status = TaskStatus.COMPLETED
                task.executed_at = datetime.utcnow()

            except Exception as e:
                logger.error(f"Task failed: {e}")
                task.status = TaskStatus.FAILED
                task.error = str(e)

            finally:
                db.commit()
