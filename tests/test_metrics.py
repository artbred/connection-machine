import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from metrics import ConnectionMachineMetrics
from tasks.comment import FeedCommentTask
from tasks.invite import InviteTask


class CommentHistoryMetricsTests(unittest.TestCase):
    def test_comment_history_entries_are_sorted_and_pruned(self):
        now = datetime.utcnow()
        payload = {
            "old-entry": {
                "author": "Old Author",
                "comment_preview": "Too old",
                "commented_at": (now - timedelta(days=45)).isoformat(),
                "post_href": "https://example.com/old",
            },
            "older-recent-entry": {
                "author": "Older Recent",
                "comment": "Still recent",
                "commented_at": (now - timedelta(days=2)).isoformat(),
                "post_href": "https://example.com/older-recent",
            },
            "newer-entry": {
                "author": "Newer Author",
                "comment": "Most recent",
                "commented_at": (now - timedelta(hours=2)).isoformat(),
                "post_href": "https://example.com/newer",
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            history_path = Path(tmpdir) / "feed_comment_history.json"
            history_path.write_text(json.dumps(payload), encoding="utf-8")

            task = object.__new__(FeedCommentTask)
            with patch("tasks.comment.COMMENT_HISTORY_PATH", history_path):
                entries = task.get_comment_history_entries()

        self.assertEqual([entry["post_key"] for entry in entries], ["newer-entry", "older-recent-entry"])
        self.assertEqual(entries[0]["author"], "Newer Author")
        self.assertIsInstance(entries[0]["commented_at"], datetime)

    def test_metrics_render_comment_history_series(self):
        metrics = ConnectionMachineMetrics(host="127.0.0.1", port=0)
        metrics.set_comment_history(
            total_comments=12,
            comments_by_day={
                "2026-04-15": 3,
                "2026-04-16": 4,
            },
            recent_entries=[
                {
                    "author": "Ada Lovelace",
                    "comment": "Thoughtful point",
                    "commented_at_iso": "2026-04-16T00:00:00",
                    "commented_at_timestamp": 1776355200.0,
                    "post_href": "https://example.com/post",
                    "post_key": "abc123",
                }
            ],
        )

        rendered = metrics.render()

        self.assertIn("connection_machine_comments_sent_total 12", rendered)
        self.assertIn(
            'connection_machine_comments_sent_by_day{day="2026-04-15"} 3',
            rendered,
        )
        self.assertIn(
            'connection_machine_comment_history_entry_timestamp_seconds{author="Ada Lovelace",comment="Thoughtful point",commented_at="2026-04-16T00:00:00",post_href="https://example.com/post",post_key="abc123"} 1776355200.0',
            rendered,
        )

    def test_invite_history_entries_are_sorted_and_pruned(self):
        now = datetime.utcnow()
        payload = {
            "old-entry": {
                "message": "Too old",
                "sent_at": (now - timedelta(days=45)).isoformat(),
                "status": "pending",
                "url": "https://example.com/old",
            },
            "older-recent-entry": {
                "message": "Still recent",
                "sent_at": (now - timedelta(days=2)).isoformat(),
                "status": "pending",
                "url": "https://example.com/older-recent",
            },
            "newer-entry": {
                "message": "Newest",
                "sent_at": (now - timedelta(hours=2)).isoformat(),
                "status": "connected",
                "url": "https://example.com/newer",
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            history_path = Path(tmpdir) / "invite_history.json"
            history_path.write_text(json.dumps(payload), encoding="utf-8")

            task = object.__new__(InviteTask)
            with patch("tasks.invite.INVITE_HISTORY_PATH", history_path):
                entries = task.get_invite_history_entries()

        self.assertEqual(
            [entry["entry_key"] for entry in entries],
            ["newer-entry", "older-recent-entry"],
        )
        self.assertEqual(entries[0]["message"], "Newest")
        self.assertIsInstance(entries[0]["sent_at"], datetime)

    def test_metrics_render_invite_history_series(self):
        metrics = ConnectionMachineMetrics(host="127.0.0.1", port=0)
        metrics.set_invite_history(
            recent_entries=[
                {
                    "entry_key": "invite-1",
                    "message": "Appreciated your post about infra reliability.",
                    "profile_url": "https://example.com/profile",
                    "sent_at_iso": "2026-04-16T01:00:00",
                    "sent_at_timestamp": 1776358800.0,
                    "status": "pending",
                }
            ],
        )

        rendered = metrics.render()

        self.assertIn(
            'connection_machine_invite_history_entry_timestamp_seconds{entry_key="invite-1",message="Appreciated your post about infra reliability.",profile_url="https://example.com/profile",sent_at="2026-04-16T01:00:00",status="pending"} 1776358800.0',
            rendered,
        )

    def test_metrics_render_invite_summary_series(self):
        metrics = ConnectionMachineMetrics(host="127.0.0.1", port=0)
        metrics.set_invite_summary(
            invites_sent_total=563,
            invites_sent_today=4,
        )

        rendered = metrics.render()

        self.assertIn("connection_machine_invites_sent_total 563", rendered)
        self.assertIn("connection_machine_invites_sent_today 4", rendered)


if __name__ == "__main__":
    unittest.main()
