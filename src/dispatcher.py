import json
import logging
import os
import random
import time

from datetime import datetime, timedelta
from db import SessionLocal, Task, TaskType, TaskStatus
from invite_state import InviteStateStore, INVITE_SKIP_COOLDOWNS
from metrics import NoopMetrics
from tasks.invite import InviteTask, normalize_invite_skip_reason
from tasks.notification_reply_invites import NotificationReplyInviteScanner
from tasks.comment import FeedCommentTask
from tasks.post import PostTask
from exceptions import SessionExpiredException, TaskSkippedException
from notifications import send_notification
from sqlalchemy import func

logger = logging.getLogger(__name__)

SKIP_COOLDOWNS: dict[tuple[TaskType, str], timedelta] = {
    **{
        (TaskType.SEND_INVITE, reason): cooldown
        for reason, cooldown in INVITE_SKIP_COOLDOWNS.items()
    },
    (TaskType.COMMENT_FEED_POST, "no_safe_commentable_posts"): timedelta(minutes=30),
}
AUTONOMOUS_COMMENT_FAILURE_COOLDOWN = timedelta(minutes=30)
COMMENT_HISTORY_DAYS = 30
COMMENT_HISTORY_RECENT_ENTRY_LIMIT = 25
INVITE_HISTORY_RECENT_ENTRY_LIMIT = 25
NOTIFICATION_REPLY_SCAN_INTERVAL = timedelta(minutes=30)

# Minimum gap between SEND_INVITE profile visits, regardless of outcome.
# Skips (audience-filter rejections, already-connected, etc.) do not consume
# the daily spacing, so without a throttle the dispatcher would burst through
# the queue one profile visit per poll while hunting for a prospect that clears
# the filter. That profile-view velocity trips LinkedIn's bot detection.
INVITE_VISIT_THROTTLE_ENV = "INVITE_MIN_VISIT_INTERVAL_SECONDS"
DEFAULT_INVITE_VISIT_THROTTLE_SECONDS = 90.0


def normalize_skip_reason(task_type: TaskType, reason: str) -> str:
    if task_type == TaskType.SEND_INVITE:
        return normalize_invite_skip_reason(reason)
    return reason


def remaining_minutes(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds() // 60))


def get_invite_visit_throttle_interval() -> timedelta | None:
    """Randomized minimum gap between SEND_INVITE profile visits.

    Read from the env on every call (0 or negative disables the throttle) so
    it can be tuned via .env without touching code. Returns None when disabled.
    """
    raw = os.getenv(INVITE_VISIT_THROTTLE_ENV, "").strip()
    base = DEFAULT_INVITE_VISIT_THROTTLE_SECONDS
    if raw:
        try:
            base = float(raw)
        except ValueError:
            logger.warning(
                "Invalid %s=%r; using default %ss",
                INVITE_VISIT_THROTTLE_ENV,
                raw,
                DEFAULT_INVITE_VISIT_THROTTLE_SECONDS,
            )
            base = DEFAULT_INVITE_VISIT_THROTTLE_SECONDS
    if base <= 0:
        return None
    return timedelta(seconds=base * random.uniform(0.7, 1.3))


def build_cooldown_notification(
    task_type: TaskType,
    reason: str,
    next_allowed: datetime,
) -> str | None:
    reason = normalize_skip_reason(task_type, reason)

    if task_type == TaskType.SEND_INVITE and reason == "weekly_limit_reached":
        until_text = next_allowed.strftime("%Y-%m-%d %H:%M UTC")
        return (
            "<b>Invite limit reached</b>\n"
            "Reason: LinkedIn weekly invitation limit\n"
            "Cooldown: 7 days\n"
            f"Resume after: {until_text}"
        )

    if task_type == TaskType.SEND_INVITE and reason == "withdrawal_cooldown":
        until_text = next_allowed.strftime("%Y-%m-%d %H:%M UTC")
        return (
            "<b>Invite temporarily blocked</b>\n"
            "Reason: LinkedIn is still withdrawing previous invitations\n"
            "Cooldown: 6 hours\n"
            f"Resume after: {until_text}"
        )

    if (
        task_type == TaskType.COMMENT_FEED_POST
        and reason == "no_safe_commentable_posts"
    ):
        until_text = next_allowed.strftime("%Y-%m-%d %H:%M UTC")
        return (
            "<b>Feed comment temporarily blocked</b>\n"
            "Reason: No safe commentable posts found in feed\n"
            "Cooldown: 30 minutes\n"
            f"Resume after: {until_text}"
        )

    return None


class TaskDispatcher:
    def __init__(self, page, metrics=None):
        self.page = page
        self.metrics = metrics or NoopMetrics()
        self.invite_state = InviteStateStore()
        self.feed_comment_handler = FeedCommentTask(page)
        self.notification_reply_invite_scanner = NotificationReplyInviteScanner(page)
        self.handlers = {
            TaskType.SEND_INVITE: InviteTask(page),
            TaskType.CREATE_POST: PostTask(page),
        }
        self.rate_limits = {
            TaskType.SEND_INVITE: 10,
            TaskType.CREATE_POST: 50,
        }
        self.autonomous_rate_limits = {
            TaskType.COMMENT_FEED_POST: 12,
        }
        self.next_execution_at: dict[TaskType, datetime] = {}
        self._invite_visit_throttle_until: datetime | None = None
        self._previously_blocked: set[TaskType] = set()
        self._last_idle_log: datetime | None = None
        self._last_notification_reply_scan: datetime | None = None
        self._logged_no_pending: bool = False
        self._init_spacing_from_db()
        self._init_autonomous_spacing()
        self._apply_durable_invite_cooldown()
        self._sync_next_execution_metrics()
        self._sync_db_task_counts()
        self._sync_comment_history_metrics()
        self._sync_invite_history_metrics()
        self._sync_invite_state_metrics()
        self._sync_notification_reply_invite_metrics()

    def _sync_next_execution_metrics(self):
        now = datetime.utcnow().timestamp()
        timestamps = {
            task_type.value: dt.timestamp()
            for task_type, dt in self.next_execution_at.items()
            if dt.timestamp() > now
        }
        self.metrics.set_next_execution_timestamps(timestamps)

    def _sync_db_task_counts(self):
        try:
            with SessionLocal() as db:
                rows = (
                    db.query(Task.type, Task.status, func.count(Task.id))
                    .group_by(Task.type, Task.status)
                    .all()
                )
            counts = {
                (task_type.value, status.value): count
                for task_type, status, count in rows
            }
            self.metrics.set_db_task_counts(counts)
        except Exception as exc:
            logger.warning("Failed to refresh DB task count metrics: %s", exc)

    def _sync_comment_history_metrics(self):
        try:
            history_entries = self.feed_comment_handler.get_comment_history_entries()
            today = datetime.utcnow().date()
            comments_by_day = {
                (today - timedelta(days=offset)).isoformat(): 0
                for offset in range(COMMENT_HISTORY_DAYS - 1, -1, -1)
            }
            for entry in history_entries:
                day = entry["commented_at"].date().isoformat()
                if day in comments_by_day:
                    comments_by_day[day] += 1

            comments_sent_today = comments_by_day.get(today.isoformat(), 0)

            recent_entries = [
                {
                    "author": entry["author"],
                    "comment": entry["comment"],
                    "commented_at_iso": entry["commented_at"].isoformat(),
                    "commented_at_timestamp": entry["commented_at"].timestamp(),
                    "post_href": entry["post_href"],
                    "post_key": entry["post_key"],
                }
                for entry in history_entries[:COMMENT_HISTORY_RECENT_ENTRY_LIMIT]
            ]
            self.metrics.set_comment_history(
                total_comments=len(history_entries),
                comments_sent_today=comments_sent_today,
                comments_by_day=comments_by_day,
                recent_entries=recent_entries,
            )
        except Exception as exc:
            logger.warning("Failed to refresh comment history metrics: %s", exc)

    def _sync_invite_history_metrics(self):
        try:
            invite_handler = self.handlers.get(TaskType.SEND_INVITE)
            if not invite_handler or not hasattr(
                invite_handler, "get_invite_history_entries"
            ):
                self.metrics.set_invite_history([])
                self.metrics.set_invite_summary(0, 0)
                return

            today_start = datetime.utcnow().replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            with SessionLocal() as db:
                invites_sent_total = (
                    db.query(Task)
                    .filter(
                        Task.type == TaskType.SEND_INVITE,
                        Task.status == TaskStatus.COMPLETED,
                    )
                    .count()
                )
                invites_sent_today = (
                    db.query(Task)
                    .filter(
                        Task.type == TaskType.SEND_INVITE,
                        Task.status == TaskStatus.COMPLETED,
                        Task.executed_at >= today_start,
                    )
                    .count()
                )

            recent_entries = [
                {
                    "entry_key": entry["entry_key"],
                    "message": entry["message"],
                    "profile_url": entry["url"],
                    "sent_at_iso": entry["sent_at"].isoformat(),
                    "sent_at_timestamp": entry["sent_at"].timestamp(),
                    "status": entry["status"],
                }
                for entry in invite_handler.get_invite_history_entries()[
                    :INVITE_HISTORY_RECENT_ENTRY_LIMIT
                ]
            ]
            self.metrics.set_invite_summary(invites_sent_total, invites_sent_today)
            self.metrics.set_invite_history(recent_entries)
        except Exception as exc:
            logger.warning("Failed to refresh invite history metrics: %s", exc)

    def _sync_invite_state_metrics(self):
        try:
            snapshot = self.invite_state.get_metrics_snapshot()
            cooldown = snapshot.get("cooldown")
            serialized_cooldown = None
            if cooldown:
                serialized_cooldown = {
                    "reason": cooldown["reason"],
                    "active_until": cooldown["active_until"].isoformat(),
                    "active_until_timestamp": cooldown["active_until"].timestamp(),
                    "source": cooldown.get("source") or "",
                    "profile_url": cooldown.get("profile_url") or "",
                }

            def serialize_event(event):
                if not event:
                    return None
                return {
                    "event_id": event.get("event_id") or "",
                    "recorded_at": event["recorded_at"].isoformat(),
                    "recorded_at_timestamp": event["recorded_at"].timestamp(),
                    "outcome": event.get("outcome") or "",
                    "reason": event.get("reason") or "",
                    "profile_url": event.get("profile_url") or "",
                    "message_preview": event.get("message_preview") or "",
                    "source": event.get("source") or "",
                    "status": event.get("status") or "",
                    "cooldown_until": (
                        event["cooldown_until"].isoformat()
                        if event.get("cooldown_until")
                        else ""
                    ),
                }

            self.metrics.set_invite_state(
                cooldown=serialized_cooldown,
                last_event=serialize_event(snapshot.get("last_event")),
                recent_events=[
                    serialize_event(event)
                    for event in snapshot.get("recent_events", [])
                    if event is not None
                ],
                event_counts=snapshot.get("event_counts", {}),
                reason_counts=snapshot.get("reason_counts", {}),
            )
        except Exception as exc:
            logger.warning("Failed to refresh invite state metrics: %s", exc)

    def _sync_notification_reply_invite_metrics(self):
        try:
            snapshot = self.notification_reply_invite_scanner.get_metrics_snapshot()
            self.metrics.set_notification_reply_invite_scan(
                last_scan_timestamp=snapshot["last_scan_timestamp"],
                latest_engagement_count=snapshot["latest_engagement_count"],
                seen_count=snapshot["seen_count"],
                queued_count=snapshot["queued_count"],
            )
        except Exception as exc:
            logger.warning(
                "Failed to refresh notification reply invite metrics: %s",
                exc,
            )

    def _apply_durable_invite_cooldown(self):
        try:
            cooldown = self.invite_state.get_active_cooldown()
        except Exception as exc:
            logger.warning("Failed to load durable invite cooldown state: %s", exc)
            return

        if not cooldown:
            return

        next_allowed = cooldown["active_until"]
        existing = self.next_execution_at.get(TaskType.SEND_INVITE)
        if existing and existing >= next_allowed:
            return

        self.next_execution_at[TaskType.SEND_INVITE] = next_allowed
        wait_min = remaining_minutes(next_allowed - datetime.utcnow())
        logger.info(
            "Applied durable invite cooldown from state: %s (~%s min remaining)",
            cooldown.get("reason") or "unknown",
            wait_min,
        )

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
                        wait_min = remaining_minutes(next_allowed - datetime.utcnow())
                        logger.info(
                            f"Restored spacing for {task_type}: ~{wait_min} min remaining"
                        )

                if task_type != TaskType.SEND_INVITE:
                    cooldown_until = self._get_restored_cooldown(db, task_type)
                    if cooldown_until:
                        restored_until = self.next_execution_at.get(task_type)
                        if not restored_until or cooldown_until > restored_until:
                            self.next_execution_at[task_type] = cooldown_until
                            wait_min = remaining_minutes(
                                cooldown_until - datetime.utcnow()
                            )
                            logger.info(
                                f"Restored cooldown for {task_type}: ~{wait_min} min remaining"
                            )

    def _init_autonomous_spacing(self):
        """Initialize spacing for non-DB autonomous actions from local history."""
        for task_type in self.autonomous_rate_limits.keys():
            last_action_at = self._get_last_autonomous_execution(task_type)
            if last_action_at:
                interval = self.get_spacing_interval(task_type)
                next_allowed = last_action_at + interval
                if next_allowed > datetime.utcnow():
                    self.next_execution_at[task_type] = next_allowed
                    wait_min = remaining_minutes(next_allowed - datetime.utcnow())
                    logger.info(
                        f"Restored spacing for {task_type}: ~{wait_min} min remaining"
                    )

    def _get_restored_cooldown(
        self,
        db,
        task_type: TaskType,
    ) -> datetime | None:
        latest_cooldown_end: datetime | None = None

        cooldowns_for_type = {
            reason: cooldown
            for (cooldown_task_type, reason), cooldown in SKIP_COOLDOWNS.items()
            if cooldown_task_type == task_type
        }
        if not cooldowns_for_type:
            return None

        max_cooldown = max(cooldowns_for_type.values())
        lookback_start = datetime.utcnow() - max_cooldown
        recent_skips = (
            db.query(Task)
            .filter(
                Task.type == task_type,
                Task.status == TaskStatus.FAILED,
                Task.executed_at.isnot(None),
                Task.executed_at >= lookback_start,
            )
            .order_by(Task.executed_at.desc())
            .all()
        )

        for skipped_task in recent_skips:
            if not skipped_task.executed_at or not skipped_task.error:
                continue

            normalized_reason = normalize_skip_reason(task_type, skipped_task.error)
            cooldown = cooldowns_for_type.get(normalized_reason)
            if not cooldown:
                continue

            cooldown_end = skipped_task.executed_at + cooldown
            if cooldown_end <= datetime.utcnow():
                continue

            if not latest_cooldown_end or cooldown_end > latest_cooldown_end:
                latest_cooldown_end = cooldown_end

        return latest_cooldown_end

    def get_spacing_interval(self, task_type: TaskType) -> timedelta:
        """Calculate randomized spacing between tasks based on rate limit."""
        limit = self.rate_limits.get(
            task_type,
            self.autonomous_rate_limits.get(task_type, 100),
        )
        base_seconds = (24 * 60 * 60) / limit  # seconds per task
        randomized = base_seconds * random.uniform(0.7, 1.3)
        return timedelta(seconds=randomized)

    def _get_last_autonomous_execution(self, task_type: TaskType) -> datetime | None:
        if task_type == TaskType.COMMENT_FEED_POST:
            timestamps = self.feed_comment_handler.get_comment_timestamps()
            return timestamps[-1] if timestamps else None
        return None

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

    def cleanup_db_backed_feed_comment_tasks(self):
        """Remove legacy DB-backed feed comment tasks. Feed comments now run autonomously."""
        with SessionLocal() as db:
            comment_tasks = (
                db.query(Task).filter(Task.type == TaskType.COMMENT_FEED_POST).all()
            )
            if not comment_tasks:
                return

            logger.warning(
                f"Deleting {len(comment_tasks)} legacy COMMENT_FEED_POST tasks from the database."
            )
            for task in comment_tasks:
                db.delete(task)
            db.commit()

    def schedule_next_execution(self, task_type: TaskType):
        """Set the next allowed execution time for a task type after completion."""
        interval = self.get_spacing_interval(task_type)
        self.next_execution_at[task_type] = datetime.utcnow() + interval
        logger.info(
            f"Next {task_type} scheduled in ~{remaining_minutes(interval)} minutes"
        )
        self._sync_next_execution_metrics()

    def _apply_invite_visit_throttle(self):
        """Impose a short gap before the next SEND_INVITE attempt.

        Called after a profile-visiting skip/failure. Skips do not consume the
        daily spacing, so without this the dispatcher bursts through the queue
        while hunting for a prospect that clears the audience filter. Only ever
        extends the block: a longer real cooldown/spacing is left untouched.
        """
        interval = get_invite_visit_throttle_interval()
        if interval is None:
            return
        throttled_until = datetime.utcnow() + interval
        spacing = self.next_execution_at.get(TaskType.SEND_INVITE)
        if spacing and spacing >= throttled_until:
            return
        if (
            self._invite_visit_throttle_until
            and self._invite_visit_throttle_until >= throttled_until
        ):
            return
        self._invite_visit_throttle_until = throttled_until

    def _invite_visit_throttle_active(self) -> bool:
        """Whether a recent invite profile visit is still cooling off."""
        return (
            self._invite_visit_throttle_until is not None
            and datetime.utcnow() < self._invite_visit_throttle_until
        )

    def schedule_skip_cooldown(self, task_type: TaskType, reason: str):
        """Apply a temporary task-type cooldown for skip reasons that indicate platform limits."""
        reason = normalize_skip_reason(task_type, reason)
        cooldown = SKIP_COOLDOWNS.get((task_type, reason))
        if not cooldown:
            return

        if task_type == TaskType.SEND_INVITE:
            active_cooldown = self.invite_state.get_active_cooldown()
            if active_cooldown and active_cooldown.get("reason") == reason:
                next_allowed = active_cooldown["active_until"]
                existing = self.next_execution_at.get(task_type)
                if not existing or existing < next_allowed:
                    self.next_execution_at[task_type] = next_allowed
                logger.warning(
                    f"{task_type} cooling down until existing invite cooldown expires due to skip reason: {reason}"
                )
                self._sync_next_execution_metrics()
                self._sync_invite_state_metrics()
                return

        next_allowed = datetime.utcnow() + cooldown
        existing = self.next_execution_at.get(task_type)
        if existing and existing > next_allowed:
            return

        self.next_execution_at[task_type] = next_allowed
        logger.warning(
            f"{task_type} cooling down for {cooldown} due to skip reason: {reason}"
        )
        if task_type == TaskType.SEND_INVITE:
            self.invite_state.set_cooldown(
                reason,
                next_allowed,
                source="dispatcher",
            )
        self._sync_next_execution_metrics()
        self._sync_invite_state_metrics()

        notification = build_cooldown_notification(task_type, reason, next_allowed)
        if notification:
            send_notification(notification)

    def can_run_autonomous_comment(self) -> bool:
        """Return whether an autonomous feed comment action is currently allowed."""
        if not os.getenv("OPENROUTER_API_KEY"):
            self.metrics.set_autonomous_comment_allowed(False)
            return False

        task_type = TaskType.COMMENT_FEED_POST
        next_allowed = self.next_execution_at.get(task_type)
        if next_allowed and datetime.utcnow() < next_allowed:
            self.metrics.set_autonomous_comment_allowed(False)
            return False

        last_24h = datetime.utcnow() - timedelta(hours=24)
        timestamps = self.feed_comment_handler.get_comment_timestamps()
        recent = [ts for ts in timestamps if ts >= last_24h]
        limit = self.autonomous_rate_limits[task_type]
        if len(recent) >= limit:
            self.next_execution_at[task_type] = min(recent) + timedelta(hours=24)
            self._sync_next_execution_metrics()
            self.metrics.set_autonomous_comment_allowed(False)
            return False

        self.metrics.set_autonomous_comment_allowed(True)
        return True

    def maybe_run_autonomous_comment(self) -> bool:
        """Run a feed comment action directly when no DB-backed task is runnable."""
        if not self.can_run_autonomous_comment():
            return False

        logger.info("Executing autonomous feed comment action")
        started_at = time.monotonic()
        outcome = "completed"
        try:
            self.feed_comment_handler.run({})
            self.schedule_next_execution(TaskType.COMMENT_FEED_POST)
        except TaskSkippedException as e:
            logger.info(f"Autonomous feed comment skipped: {e.reason}")
            self.schedule_skip_cooldown(TaskType.COMMENT_FEED_POST, e.reason)
            outcome = "skipped"
        except Exception as e:
            logger.error(f"Autonomous feed comment failed: {e}")
            self.next_execution_at[TaskType.COMMENT_FEED_POST] = (
                datetime.utcnow() + AUTONOMOUS_COMMENT_FAILURE_COOLDOWN
            )
            self._sync_next_execution_metrics()
            outcome = "failed"
        self._sync_comment_history_metrics()
        self.metrics.observe_task(
            TaskType.COMMENT_FEED_POST.value,
            outcome,
            time.monotonic() - started_at,
        )
        return True

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
                wait_min = remaining_minutes(next_allowed - datetime.utcnow())
                logger.info(f"{task_type} blocked (next in ~{wait_min} min)")
            else:
                logger.info(f"{task_type} blocked (rate limit reached)")

        for task_type in newly_unblocked:
            logger.info(f"{task_type} unblocked, ready to execute")

        self._previously_blocked = blocked_set
        return blocked

    def is_task_type_blocked(self, task_type: TaskType) -> bool:
        last_24h = datetime.utcnow() - timedelta(hours=24)
        limit = self.rate_limits.get(task_type)
        if limit is None:
            return False

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
                return True

        next_allowed = self.next_execution_at.get(task_type)
        return bool(next_allowed and datetime.utcnow() < next_allowed)

    def poll(self):
        """Fetch and execute pending tasks."""
        self._apply_durable_invite_cooldown()
        self.metrics.mark_poll()
        self._sync_db_task_counts()
        self._sync_comment_history_metrics()
        self._sync_invite_history_metrics()
        self._sync_invite_state_metrics()
        self._sync_notification_reply_invite_metrics()

        # Get distinct pending task types first
        with SessionLocal() as db:
            pending_types = set(
                row[0]
                for row in db.query(Task.type)
                .filter(
                    Task.status == TaskStatus.PENDING,
                    Task.type.in_(tuple(self.handlers.keys())),
                )
                .distinct()
                .all()
            )

        blocked_types = self.get_rate_limited_types(pending_types)

        # Short per-visit throttle: block SEND_INVITE while a recent profile
        # visit is cooling off. Kept out of get_rate_limited_types so it does
        # not spam the block/unblock log on every cycle.
        if (
            TaskType.SEND_INVITE in pending_types
            and TaskType.SEND_INVITE not in blocked_types
            and self._invite_visit_throttle_active()
        ):
            blocked_types = [*blocked_types, TaskType.SEND_INVITE]

        should_scan_notification_replies = not self.is_task_type_blocked(
            TaskType.SEND_INVITE
        ) and (
            self._last_notification_reply_scan is None
            or datetime.utcnow() - self._last_notification_reply_scan
            >= NOTIFICATION_REPLY_SCAN_INTERVAL
        )
        if should_scan_notification_replies:
            self._last_notification_reply_scan = datetime.utcnow()
            try:
                self.notification_reply_invite_scanner.run({})
                self._sync_notification_reply_invite_metrics()
            except Exception as exc:
                logger.warning("Notification reply invite scan failed: %s", exc)

        with SessionLocal() as db:
            query = db.query(Task).filter(
                Task.status == TaskStatus.PENDING,
                Task.type.in_(tuple(self.handlers.keys())),
            )
            if blocked_types:
                query = query.filter(Task.type.notin_(blocked_types))

            task_to_run = query.order_by(Task.created_at).first()

            if not task_to_run:
                if self.maybe_run_autonomous_comment():
                    self._last_idle_log = None
                    self._logged_no_pending = False
                    return

                now = datetime.utcnow()

                # Log "no pending tasks" once when queue is empty
                if not pending_types and not self._logged_no_pending:
                    logger.info("Idle: no pending tasks")
                    self._logged_no_pending = True

                # Log rate-limited status periodically (every 5 minutes)
                if blocked_types:
                    should_log = self._last_idle_log is None or (
                        now - self._last_idle_log
                    ) > timedelta(minutes=5)
                    if should_log:
                        logger.info(
                            f"Waiting: {len(blocked_types)} task type(s) rate-limited"
                        )
                        self._last_idle_log = now

                return

            # Reset flags when we have work to do
            self._last_idle_log = None
            self._logged_no_pending = False
            logger.info(f"Executing: {task_to_run}")

            task_to_run.status = TaskStatus.PROCESSING
            db.commit()

            outcome = "completed"
            started_at = time.monotonic()
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
                normalized_reason = normalize_skip_reason(task_to_run.type, e.reason)
                if normalized_reason != e.reason:
                    logger.info(
                        "Task %s skipped: %s (normalized to %s)",
                        task_to_run.id,
                        e.reason,
                        normalized_reason,
                    )
                else:
                    logger.info(f"Task {task_to_run.id} skipped: {e.reason}")
                if e.from_active_cooldown:
                    task_to_run.status = TaskStatus.PENDING
                    task_to_run.error = None
                    cooldown_until = e.cooldown_until
                    if cooldown_until and cooldown_until > datetime.utcnow():
                        self.next_execution_at[task_to_run.type] = cooldown_until
                        self._sync_next_execution_metrics()
                    outcome = "blocked"
                else:
                    task_to_run.status = TaskStatus.FAILED
                    task_to_run.error = normalized_reason
                    task_to_run.executed_at = datetime.utcnow()
                    if e.cooldown_eligible:
                        self.schedule_skip_cooldown(task_to_run.type, normalized_reason)
                    # Most skips do not count toward rate limits; platform-limit skips may set a cooldown
                    if task_to_run.type == TaskType.SEND_INVITE:
                        self._apply_invite_visit_throttle()
                    outcome = "skipped"

            except SessionExpiredException as e:
                logger.warning(f"Session expired during task {task_to_run.id}: {e}")
                task_to_run.status = TaskStatus.PENDING
                outcome = "session_expired"
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
                is_session_error = any(
                    indicator in error_str for indicator in session_indicators
                )

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
                    logger.warning(
                        f"Detected session issue during task {task_to_run.id}: {e}"
                    )
                    task_to_run.status = TaskStatus.PENDING
                    db.commit()
                    outcome = "session_expired"
                    raise SessionExpiredException(f"Session issue detected: {e}")

                logger.error(f"Task failed: {e}")
                task_to_run.status = TaskStatus.FAILED
                task_to_run.error = normalize_skip_reason(task_to_run.type, str(e))
                task_to_run.executed_at = datetime.utcnow()
                if task_to_run.type == TaskType.SEND_INVITE:
                    self._apply_invite_visit_throttle()
                outcome = "failed"

            finally:
                db.commit()
                self.metrics.observe_task(
                    task_to_run.type.value,
                    outcome,
                    time.monotonic() - started_at,
                )
                self._sync_db_task_counts()
                self._sync_comment_history_metrics()
                self._sync_invite_history_metrics()
                self._sync_invite_state_metrics()
                self._sync_notification_reply_invite_metrics()
                try:
                    state_payload = json.loads(task_to_run.payload)
                    if state_payload.get("source") == "notification_reply":
                        self.notification_reply_invite_scanner.mark_task_status(
                            task_to_run.id,
                            task_to_run.status.value,
                        )
                except Exception as exc:
                    logger.warning(
                        "Failed to update notification reply task state: %s",
                        exc,
                    )
