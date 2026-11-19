import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, call, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import dispatcher as dispatch  # noqa: E402
import main  # noqa: E402
from db import Base, Task, TaskStatus, TaskType  # noqa: E402
from exceptions import SessionExpiredException, TaskSkippedException  # noqa: E402
from invite_state import InviteStateStore  # noqa: E402


class Clock(datetime):
    current = datetime(2030, 1, 1, 12)

    @classmethod
    def utcnow(cls):
        return cls.current


class DispatcherSchedulingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.engine = create_engine(f"sqlite:///{directory.name}/queue.db")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )
        Clock.current = datetime(2030, 1, 1, 12)
        self.state = InviteStateStore(Path(directory.name) / "invite-state.json")
        self.actions = []
        self.action_pages = []
        self.failures = {}
        self.duration = timedelta(0)
        self.preflight_safe = True
        owner = self

        class Handler:
            def __init__(self, kind, page):
                self.kind = kind
                self.page = page

            def run(self, payload):
                owner.actions.append(self.kind)
                owner.action_pages.append(self.page)
                Clock.current += owner.duration
                reason = owner.failures.get(payload.get("url"))
                if isinstance(reason, Exception):
                    raise reason
                if reason:
                    raise TaskSkippedException(
                        reason,
                        cooldown_eligible=False,
                        retryable_preflight=owner.preflight_safe
                        and reason in dispatch.RETRYABLE_PREFLIGHT_REASONS,
                    )

            def get_invite_history_entries(self):
                return []

            def get_comment_history_entries(self):
                return []

            def get_comment_timestamps(self):
                return []

            def get_metrics_snapshot(self):
                return {
                    "last_scan_timestamp": 0,
                    "latest_engagement_count": 0,
                    "seen_count": 0,
                    "queued_count": 0,
                }

        self.patches = [
            patch.object(dispatch, "SessionLocal", self.sessions),
            patch.object(dispatch, "datetime", Clock),
            patch.object(dispatch, "InviteStateStore", return_value=self.state),
            patch.object(
                dispatch, "InviteTask", side_effect=lambda page: Handler("invite", page)
            ),
            patch.object(
                dispatch, "PostTask", side_effect=lambda page: Handler("post", page)
            ),
            patch.object(
                dispatch,
                "FeedCommentTask",
                side_effect=lambda page: Handler("comment", page),
            ),
            patch.object(
                dispatch,
                "NotificationReplyInviteScanner",
                side_effect=lambda page: Handler("scan", page),
            ),
            patch.object(
                dispatch,
                "get_invite_visit_throttle_interval",
                return_value=timedelta(seconds=90),
            ),
            patch.dict(
                os.environ,
                {"OPENROUTER_API_KEY": "offline", "TELEGRAM_NOTIFICATIONS_URL": ""},
            ),
        ]
        for active in self.patches:
            active.start()
            self.addCleanup(active.stop)
        self.worker = dispatch.TaskDispatcher(object())

    def add_task(
        self,
        slug="target",
        *,
        status=TaskStatus.PENDING,
        completed=None,
        task_type=TaskType.SEND_INVITE,
        not_before=None,
        created=None,
    ):
        with self.sessions() as db:
            row = Task(
                type=task_type,
                status=status,
                payload=json.dumps({"url": f"https://www.linkedin.com/in/{slug}/"}),
                created_at=created or Clock.current,
                executed_at=completed,
                not_before=not_before,
            )
            db.add(row)
            db.commit()
            return row.id

    def row(self, task_id):
        with self.sessions() as db:
            return db.get(Task, task_id)

    def seed_success(self, when):
        return self.add_task(
            f"sent-{when.isoformat()}", status=TaskStatus.COMPLETED, completed=when
        )

    def run_worker(self, wait):
        def dispatcher(**kwargs):
            self.worker = dispatch.TaskDispatcher(**kwargs)
            return self.worker

        shutdown = Mock()
        shutdown.is_set.return_value = False
        shutdown.wait.side_effect = wait
        with (
            patch.object(sys, "argv", ["worker"]),
            patch.object(main, "init_db"),  # setUp already initialized the isolated queue
            patch.object(main.signal, "signal"),
            patch.object(main, "shutdown_event", shutdown),
            patch.object(main, "TaskDispatcher", dispatcher),
            patch.dict(os.environ, METRICS_ENABLED="false"),
        ):
            main.main()

    def test_worker_stays_browser_free_from_start_during_durable_invite_limits(self):
        task = self.add_task()
        for hours in range(10):
            self.seed_success(Clock.current - timedelta(hours=hours))

        for limit in ("rolling quota", "cooldown"):
            with self.subTest(limit=limit), patch.dict(os.environ, OPENROUTER_API_KEY=""):
                if limit == "cooldown":
                    Clock.current += timedelta(days=2)
                    self.state.set_cooldown(
                        "weekly_limit_reached", Clock.current + timedelta(days=7), source="test"
                    )
                idle_handlers = []

                def wait(seconds):
                    self.assertEqual(seconds, 10)
                    idle_handlers.append(self.worker.handlers)
                    Clock.current += timedelta(minutes=31)
                    return len(idle_handlers) == 3

                with patch.object(main, "sync_playwright", side_effect=AssertionError) as start:
                    self.run_worker(wait)
                start.assert_not_called()
                self.assertEqual(len(idle_handlers), 3)
                self.assertTrue(all(group is idle_handlers[0] for group in idle_handlers))
                self.assertEqual(self.row(task).status, TaskStatus.PENDING)
                self.assertEqual(self.actions, [])

    def test_worker_releases_each_browser_poll_even_on_reauth_failure(self):
        invite = self.add_task()
        post = self.add_task("post", task_type=TaskType.CREATE_POST)
        self.failures["https://www.linkedin.com/in/post/"] = SessionExpiredException()
        pages = [object(), object()]
        contexts = [Mock(pages=[page]) for page in pages]

        with (
            patch.object(main, "sync_playwright") as start,
            patch.object(main, "check_ip") as check_ip,
            patch.object(main, "authenticate", side_effect=[None, None, RuntimeError("reauth")]) as auth,
        ):
            driver = start.return_value
            driver.__enter__.return_value.webkit.launch_persistent_context.side_effect = contexts

            def assert_released(index):
                contexts[index].close.assert_called_once_with()
                self.assertEqual(driver.__exit__.call_count, index + 1)
                self.assertIsNone(self.worker.page)
                for handler in (
                    self.worker.feed_comment_handler, self.worker.notification_reply_invite_scanner,
                    *self.worker.handlers.values(),
                ):
                    self.assertIsNone(handler.page)

            def wait(seconds):
                self.assertEqual(seconds, 10)
                assert_released(0)
                return False

            with self.assertRaisesRegex(RuntimeError, "reauth"):
                self.run_worker(wait)
            assert_released(1)
            self.assertEqual(start.call_count, 2)
            check_ip.assert_called_once_with(pages[0])
            self.assertEqual(auth.call_args_list, [call(pages[0]), call(pages[1]), call(pages[1], retry_delay=60)])
        self.assertEqual(self.action_pages, pages)
        self.assertEqual(self.row(invite).status, TaskStatus.COMPLETED)
        self.assertEqual(self.row(post).status, TaskStatus.PENDING)

    def test_only_due_invites_acquire_and_failed_login_leaves_them_pending(self):
        page = object()
        observed_status = []
        due = Clock.current + timedelta(minutes=5)
        task = self.add_task(not_before=due)

        def acquire():
            observed_status.append(self.row(task).status)
            if len(observed_status) == 1:
                raise SessionExpiredException()
            return page

        worker = dispatch.TaskDispatcher(acquire_page=acquire)
        worker.poll()
        self.assertEqual(observed_status, [])

        Clock.current = due
        with self.assertRaises(SessionExpiredException):
            worker.poll()
        self.assertEqual(self.row(task).status, TaskStatus.PENDING)
        self.assertIsNone(self.row(task).executed_at)
        self.assertEqual(self.actions, [])
        worker.poll()
        self.assertEqual(self.row(task).status, TaskStatus.COMPLETED)
        self.assertEqual(self.action_pages, [page])
        self.assertEqual(observed_status, [TaskStatus.PENDING, TaskStatus.PENDING])

    def test_scan_and_comment_rebind_page_and_retry_after_login_failure(self):
        scan, comment = object(), object()
        acquire = Mock(
            side_effect=[SessionExpiredException(), scan, SessionExpiredException(), comment]
        )
        worker = dispatch.TaskDispatcher(acquire_page=acquire)
        for expected in ("scan", "comment"):
            with self.assertRaises(SessionExpiredException):
                worker.poll()
            worker.poll()
            self.assertEqual(self.actions[-1], expected)
        self.assertEqual(self.action_pages, [scan, comment])
        worker.poll()
        self.assertEqual(acquire.call_count, 4)

    def test_due_invite_precedes_older_post_and_notification_scan(self):
        post = self.add_task(
            "post",
            task_type=TaskType.CREATE_POST,
            created=Clock.current - timedelta(days=1),
        )
        invite = self.add_task()
        started = Clock.current
        self.worker.poll()
        self.assertEqual(self.row(invite).status, TaskStatus.COMPLETED)
        self.assertEqual(self.row(post).status, TaskStatus.PENDING)
        self.assertEqual(self.actions, ["invite"])
        # The just-completed, initially uncommitted row must consume its slot.
        self.assertGreaterEqual(
            self.worker.next_execution_at[TaskType.SEND_INVITE],
            started + timedelta(minutes=100, seconds=48),
        )

    def test_rolling_cap_includes_exact_boundary_then_releases_one_slot(self):
        now = Clock.current
        oldest = now - timedelta(hours=24)
        for offset in (0, 4, 6, 8, 10, 12, 14, 16, 18, 21):
            self.seed_success(oldest + timedelta(hours=offset))
        pending = self.add_task()
        self.worker.poll()
        self.assertEqual(self.row(pending).status, TaskStatus.PENDING)
        self.assertEqual(self.actions, [])
        Clock.current += timedelta(microseconds=1)
        self.worker.poll()
        self.assertEqual(self.row(pending).status, TaskStatus.COMPLETED)
        with self.sessions() as db:
            count = (
                db.query(Task)
                .filter(
                    Task.status == TaskStatus.COMPLETED,
                    Task.executed_at >= Clock.current - timedelta(hours=24),
                )
                .count()
            )
        self.assertEqual(count, 10)

    def test_current_minimum_gap_is_preserved_when_quota_is_underused(self):
        last = Clock.current - timedelta(minutes=90)
        for offset in (22, 18, 14, 10):
            self.seed_success(Clock.current - timedelta(hours=offset))
        self.seed_success(last)
        pending = self.add_task()
        self.worker.poll()
        self.assertEqual(self.row(pending).status, TaskStatus.PENDING)
        self.assertEqual(self.actions, [])
        self.assertEqual(
            self.worker.next_execution_at[TaskType.SEND_INVITE],
            last + timedelta(minutes=100.8),
        )

    def test_completion_overhead_does_not_restart_a_full_spacing_period(self):
        oldest = Clock.current - timedelta(hours=3)
        self.seed_success(oldest)
        pending = self.add_task()
        self.duration = timedelta(minutes=12)
        self.worker.poll()
        self.assertEqual(self.row(pending).status, TaskStatus.COMPLETED)
        self.assertGreaterEqual(
            self.worker.next_execution_at[TaskType.SEND_INVITE],
            Clock.current + timedelta(minutes=100, seconds=48),
        )
        self.assertLess(
            self.worker.next_execution_at[TaskType.SEND_INVITE],
            Clock.current + timedelta(hours=2.4),
        )

    def test_background_work_is_held_for_an_imminent_invite(self):
        self.seed_success(Clock.current - timedelta(minutes=90))
        pending = self.add_task()
        self.worker.poll()
        self.assertEqual(self.row(pending).status, TaskStatus.PENDING)
        self.assertEqual(self.actions, [])

    def test_background_scan_moves_into_idle_time_without_repeating_per_slot(self):
        self.seed_success(Clock.current - timedelta(minutes=20))
        pending = self.add_task()
        self.worker.poll()
        self.assertEqual(self.actions, ["scan"])
        self.assertEqual(self.row(pending).status, TaskStatus.PENDING)
        self.worker.poll()
        self.assertEqual(self.actions, ["scan", "comment"])
        Clock.current += timedelta(minutes=31)
        self.worker.poll()
        self.assertEqual(self.actions, ["scan", "comment"])
        self.assertEqual(self.row(pending).status, TaskStatus.PENDING)

    def test_preflight_retry_is_durable_and_does_not_block_other_candidates(self):
        slow = self.add_task("slow")
        next_target = self.add_task(
            "ready", created=Clock.current + timedelta(seconds=1)
        )
        self.failures["https://www.linkedin.com/in/slow/"] = "audience_unavailable"
        initial = Clock.current
        self.worker.poll()
        deferred = self.row(slow)
        self.assertEqual(deferred.status, TaskStatus.PENDING)
        self.assertEqual(deferred.not_before, initial + timedelta(minutes=30))
        self.assertEqual(deferred.preflight_retries, 1)
        self.assertIsNone(deferred.executed_at)
        Clock.current += timedelta(seconds=91)
        restarted = dispatch.TaskDispatcher(object())
        restarted.poll()
        self.assertEqual(self.row(next_target).status, TaskStatus.COMPLETED)
        self.assertEqual(self.row(slow).not_before, deferred.not_before)
        self.assertEqual(self.row(slow).preflight_retries, 1)

    def test_preflight_retries_are_bounded_across_restarts(self):
        task_id = self.add_task("slow")
        self.failures["https://www.linkedin.com/in/slow/"] = "profile_not_ready"
        initial = Clock.current
        self.worker.poll()
        Clock.current = initial + timedelta(minutes=30)
        self.worker = dispatch.TaskDispatcher(object())
        self.worker.poll()
        second = self.row(task_id)
        self.assertEqual(second.preflight_retries, 2)
        self.assertEqual(second.not_before, Clock.current + timedelta(hours=6))
        Clock.current = second.not_before
        self.worker = dispatch.TaskDispatcher(object())
        self.worker.poll()
        final = self.row(task_id)
        self.assertEqual(final.status, TaskStatus.FAILED)
        self.assertEqual(final.error, "profile_not_ready")
        self.assertEqual(final.preflight_retries, 2)
        self.assertIsNone(final.not_before)
        self.assertEqual(self.actions, ["invite", "invite", "invite"])

    def test_rejections_and_uncertain_sends_are_never_requeued(self):
        for reason in (
            "audience_filter",
            "invite_not_confirmed",
            "profile_identity_mismatch",
            "modal_recipient_mismatch",
            "selector_timeout",
            "navigation_timeout",
        ):
            with self.subTest(reason=reason):
                task_id = self.add_task(reason)
                self.failures[f"https://www.linkedin.com/in/{reason}/"] = reason
                self.worker = dispatch.TaskDispatcher(object())
                self.worker.poll()
                row = self.row(task_id)
                self.assertEqual(row.status, TaskStatus.FAILED)
                self.assertEqual(row.error, reason)
                self.assertEqual(row.preflight_retries, 0)
                self.assertIsNone(row.not_before)
                Clock.current += timedelta(days=1, seconds=1)

    def test_restart_keeps_quota_deadline_and_durable_cooldown(self):
        self.seed_success(Clock.current - timedelta(minutes=20))
        self.add_task()
        deadline = self.worker._refresh_invite_schedule()
        restarted = dispatch.TaskDispatcher(object())
        self.assertEqual(restarted.next_execution_at[TaskType.SEND_INVITE], deadline)
        cooldown = Clock.current + timedelta(days=7)
        self.state.set_cooldown("weekly_limit_reached", cooldown, source="test")
        restarted = dispatch.TaskDispatcher(object())
        restarted.poll()
        self.assertEqual(restarted.next_execution_at[TaskType.SEND_INVITE], cooldown)
        self.assertNotIn("invite", self.actions)
        self.assertNotIn("scan", self.actions)

    def test_not_before_applies_even_when_send_quota_is_empty(self):
        due = Clock.current + timedelta(minutes=10)
        task_id = self.add_task(not_before=due)
        self.worker.poll()
        self.assertEqual(self.row(task_id).status, TaskStatus.PENDING)
        self.assertEqual(self.actions, [])
        Clock.current = due
        self.worker.poll()
        self.assertEqual(self.row(task_id).status, TaskStatus.COMPLETED)

    def test_reason_alone_never_authorizes_a_preflight_retry(self):
        task_id = self.add_task("unconfirmed-read")
        self.failures["https://www.linkedin.com/in/unconfirmed-read/"] = (
            "profile_not_ready"
        )
        self.preflight_safe = False
        self.worker.poll()
        task = self.row(task_id)
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(task.preflight_retries, 0)
        self.assertIsNone(task.not_before)

    def test_unconfirmed_attempt_conservatively_consumes_a_quota_slot(self):
        oldest = Clock.current - timedelta(hours=20)
        for index in range(9):
            self.seed_success(oldest + timedelta(hours=index * 2))
        uncertain = self.add_task(
            "uncertain",
            status=TaskStatus.FAILED,
            completed=Clock.current - timedelta(hours=2),
        )
        with self.sessions() as db:
            row = db.get(Task, uncertain)
            row.error = "invite_not_confirmed"
            db.commit()
        pending = self.add_task()
        self.worker.poll()
        self.assertEqual(self.row(pending).status, TaskStatus.PENDING)
        self.assertNotIn("invite", self.actions)
        self.assertEqual(
            self.worker.next_execution_at[TaskType.SEND_INVITE],
            oldest + timedelta(hours=24, microseconds=1),
        )

    def test_interrupted_invites_are_held_not_replayed_on_restart(self):
        uncertain = self.add_task("interrupted", status=TaskStatus.PROCESSING)
        post = self.add_task(
            "post", status=TaskStatus.PROCESSING, task_type=TaskType.CREATE_POST
        )
        self.worker.cleanup_zombie_tasks()
        held = self.row(uncertain)
        self.assertEqual(held.status, TaskStatus.FAILED)
        self.assertEqual(held.error, "invite_not_confirmed")
        self.assertEqual(held.executed_at, Clock.current)
        self.assertEqual(self.row(post).status, TaskStatus.PENDING)
        self.assertIn(
            TaskType.SEND_INVITE,
            self.worker.get_rate_limited_types({TaskType.SEND_INVITE}),
        )


if __name__ == "__main__":
    unittest.main()
