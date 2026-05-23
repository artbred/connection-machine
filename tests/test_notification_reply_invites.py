import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from connection_state import ConnectionState  # noqa: E402
from db import Base, SessionLocal, Task, TaskStatus, TaskType, engine  # noqa: E402
from human_actions import HumanActions  # noqa: E402
from playwright.sync_api import Page  # noqa: E402
from tasks.notification_reply_invites import (  # noqa: E402
    MAX_LATEST_COMMENT_ENGAGEMENTS,
    NOTIFICATION_REPLY_INVITE_SOURCE,
    NotificationReplyInviteScanner,
    is_comment_engagement_notification,
    notification_key,
    normalize_profile_url,
)


class FakeHuman:
    def random_sleep(self, *_args, **_kwargs):
        pass


class FakePage:
    url = "https://www.linkedin.com/feed/"

    def __init__(self, raw_candidates=None):
        self.raw_candidates = raw_candidates or []
        self.visited = []

    def goto(self, url, **_kwargs):
        self.url = url
        self.visited.append(url)

    def wait_for_selector(self, *_args, **_kwargs):
        pass

    def evaluate(self, _script, _payload=None):
        return self.raw_candidates

    def locator(self, _selector):
        return FakeLocatorList()


class FakeLocatorList:
    def count(self):
        return 0


class FakeScanner(NotificationReplyInviteScanner):
    def __init__(self, candidates, state_path):
        self.page = cast(Page, FakePage())
        self.human = cast(HumanActions, FakeHuman())
        self.state_path = state_path
        self.candidates = candidates

    def validate_session(self):
        pass

    def find_reply_candidates(self, max_candidates=20):
        return self.candidates

    def _get_connection_state(self, profile_url):
        return ConnectionState.CONNECTABLE


class NotificationReplyInviteTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        with SessionLocal() as db:
            db.query(Task).delete()
            db.commit()

    def test_extract_reply_candidates_normalizes_and_dedupes_profiles(self):
        page = FakePage(
            [
                {
                    "profile_url": "https://www.linkedin.com/in/ada/?miniProfileUrn=abc",
                    "name": " Ada Lovelace ",
                    "text": "Ada replied to your comment on their post",
                },
                {
                    "profile_url": "https://www.linkedin.com/in/ada/",
                    "name": "Ada Lovelace",
                    "text": "Ada loved your comment",
                },
                {
                    "profile_url": "https://www.linkedin.com/company/example/",
                    "name": "Example",
                    "text": "Company replied to your comment on their post",
                },
            ]
        )
        scanner = NotificationReplyInviteScanner(page)
        scanner.human = cast(HumanActions, FakeHuman())

        candidates = scanner._extract_reply_candidates()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0]["profile_url"], "https://www.linkedin.com/in/ada/"
        )
        self.assertEqual(candidates[0]["name"], "Ada Lovelace")

    def test_comment_engagement_regex_matches_requested_markers(self):
        self.assertTrue(is_comment_engagement_notification("Ada loved your comment"))
        self.assertTrue(is_comment_engagement_notification("Ada reposted your comment"))
        self.assertTrue(
            is_comment_engagement_notification("Ada replied to your comment")
        )
        self.assertFalse(is_comment_engagement_notification("Ada liked your post"))

    def test_notification_key_ignores_status_presence_text(self):
        profile_url = "https://www.linkedin.com/in/ada/"

        self.assertEqual(
            notification_key(profile_url, "Status is online Ada loved your comment"),
            notification_key(profile_url, "Status is offline Ada loved your comment"),
        )

    def test_find_reply_candidates_returns_only_latest_ten_engagements(self):
        page = FakePage(
            [
                {
                    "profile_url": f"https://www.linkedin.com/in/person-{index}/",
                    "name": f"Person {index}",
                    "text": f"Person {index} loved your comment",
                }
                for index in range(12)
            ]
        )
        scanner = NotificationReplyInviteScanner(page)
        scanner.human = cast(HumanActions, FakeHuman())

        candidates = scanner.find_reply_candidates(12)

        self.assertEqual(len(candidates), MAX_LATEST_COMMENT_ENGAGEMENTS)
        self.assertEqual(
            candidates[0]["profile_url"], "https://www.linkedin.com/in/person-0/"
        )
        self.assertEqual(
            candidates[-1]["profile_url"], "https://www.linkedin.com/in/person-9/"
        )

    def test_seen_latest_engagements_noop_without_profile_visits(self):
        candidate = {
            "profile_url": normalize_profile_url("https://www.linkedin.com/in/ada/"),
            "name": "Ada Lovelace",
            "text": "Ada replied to your comment",
            "notification_key": "notif-1",
        }

        class SeenScanner(FakeScanner):
            profile_visits = 0

            def _get_connection_state(self, profile_url):
                self.profile_visits += 1
                return ConnectionState.CONNECTABLE

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "profiles": {},
                        "seen_notifications": {"notif-1": {"status": "queued"}},
                        "last_scan": {},
                    }
                ),
                encoding="utf-8",
            )
            scanner = SeenScanner([candidate], state_path)

            with SessionLocal() as db:
                queued = scanner.queue_reply_invites(db)

            self.assertEqual(queued, [])
            self.assertEqual(scanner.profile_visits, 0)
            last_scan = json.loads(state_path.read_text(encoding="utf-8"))["last_scan"]
            self.assertEqual(last_scan["latest_engagement_count"], 1)
            self.assertEqual(last_scan["seen_count"], 1)
            self.assertEqual(last_scan["queued_count"], 0)

    def test_mark_task_status_updates_profile_and_seen_records(self):
        candidate = {
            "profile_url": normalize_profile_url("https://www.linkedin.com/in/ada/"),
            "name": "Ada Lovelace",
            "text": "Ada replied to your comment",
            "notification_key": "notif-1",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            scanner = FakeScanner([candidate], state_path)
            with SessionLocal() as db:
                queued = scanner.queue_reply_invites(db)

            scanner.mark_task_status(queued[0]["task_id"], "completed")

            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                state["profiles"]["https://www.linkedin.com/in/ada/"]["status"],
                "completed",
            )
            self.assertEqual(
                state["seen_notifications"]["notif-1"]["status"],
                "completed",
            )

    def test_queue_reply_invite_creates_prioritized_send_invite_task(self):
        candidate = {
            "profile_url": normalize_profile_url("https://www.linkedin.com/in/ada/"),
            "name": "Ada Lovelace",
            "text": "Ada replied to your comment on their post",
            "notification_key": "notif-1",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            scanner = FakeScanner([candidate], Path(tmpdir) / "state.json")

            with SessionLocal() as db:
                normal_task = Task(
                    type=TaskType.SEND_INVITE,
                    payload=json.dumps({"url": "https://www.linkedin.com/in/grace/"}),
                    status=TaskStatus.PENDING,
                    created_at=datetime.utcnow(),
                )
                db.add(normal_task)
                db.commit()

                queued = scanner.queue_reply_invites(db)
                next_task = (
                    db.query(Task)
                    .filter(Task.status == TaskStatus.PENDING)
                    .order_by(Task.created_at)
                    .first()
                )

            self.assertEqual(len(queued), 1)
            self.assertIsNotNone(next_task)
            payload = json.loads(next_task.payload)
            self.assertEqual(payload["url"], "https://www.linkedin.com/in/ada/")
            self.assertEqual(payload["source"], NOTIFICATION_REPLY_INVITE_SOURCE)


if __name__ == "__main__":
    unittest.main()
