import json
import logging
from datetime import datetime, timedelta
from sqlalchemy import or_
from db import SessionLocal, Task, TaskType, TaskStatus
from tasks.invite import InviteTask
from tasks.post import PostTask
from exceptions import SessionExpiredException

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

    def cleanup_zombie_tasks(self):
        """Reset tasks that were stuck in PROCESSING state (e.g. due to crash)."""
        with SessionLocal() as db:
            zombies = (
                db.query(Task)
                .filter(Task.status == TaskStatus.PROCESSING)
                .all()
            )
            if zombies:
                logger.warning(f"Found {len(zombies)} zombie tasks. Resetting to PENDING.")
                for task in zombies:
                    task.status = TaskStatus.PENDING
                db.commit()

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
            # Get pending tasks, ordered by priority/time
            # We fetch a batch to find one that isn't rate limited
            tasks = (
                db.query(Task)
                .filter(
                    Task.status == TaskStatus.PENDING,
                    or_(Task.scheduled_for.is_(None), Task.scheduled_for <= datetime.utcnow())
                )
                .order_by(Task.created_at)
                .limit(10) # Fetch top 10 to avoid blocking if first one is rate limited
                .all()
            )

            if not tasks:
                return

            task_to_run = None
            for task in tasks:
                if self.check_rate_limit(task.type):
                    task_to_run = task
                    break
            
            if not task_to_run:
                logger.info("All pending tasks are currently rate limited.")
                return

            logger.info(f"Found task: {task_to_run}")

            # Mark as processing
            task_to_run.status = TaskStatus.PROCESSING
            db.commit()

            try:
                handler = self.handlers.get(task_to_run.type)
                if not handler:
                    raise ValueError(f"No handler for task type: {task_to_run.type}")

                payload = json.loads(task_to_run.payload)
                handler.run(payload)

                # Mark as completed
                task_to_run.status = TaskStatus.COMPLETED
                task_to_run.executed_at = datetime.utcnow()
                
            except SessionExpiredException as e:
                logger.warning(f"Session expired during task {task_to_run.id}: {e}")
                # Reset task to PENDING so it can be retried after re-auth
                task_to_run.status = TaskStatus.PENDING
                db.commit()
                raise e

            except Exception as e:
                logger.error(f"Task failed: {e}")
                task_to_run.status = TaskStatus.FAILED
                task_to_run.error = str(e)
                db.commit()

            finally:
                db.commit()
