import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from dom_minifier import minify_dom
from state_json import write_state_json
from tasks.notification_reply_invites import NotificationReplyInviteScanner


class ResourceBoundsTests(unittest.TestCase):
    def test_dom_budget_counts_visible_output_not_discarded_markup(self):
        button = '<button aria-label="Connect with Ada">Connect</button>'
        html = '<svg>' + 'discard' * 10000 + '</svg>' + button.replace('with Ada', 'with\n  Ada')
        self.assertEqual(minify_dom(html, len(button)), button)
        self.assertEqual(minify_dom(html + '<div>More</div>', len(button) + 2),
                         button + '...[truncated]')

    def test_state_encoding_failure_keeps_previous_snapshot_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text('{"original":true}')
            path.chmod(0o640)
            with self.assertRaises(TypeError):
                write_state_json(path, {"items": ["valid", object()]})
            self.assertEqual(json.loads(path.read_text()), {"original": True})
            self.assertEqual(list(path.parent.iterdir()), [path])
            write_state_json(path, {"items": ["Ada", "雪"]})
            self.assertEqual(json.loads(path.read_text()), {"items": ["Ada", "雪"]})
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_scan_metrics_observe_replacement_and_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            scanner = NotificationReplyInviteScanner(None, path)
            write_state_json(path, {"profiles": {}, "last_scan": {"queued_count": 1}})
            modified = path.stat().st_mtime_ns
            self.assertEqual(scanner.get_metrics_snapshot()['queued_count'], 1)
            write_state_json(path, {"profiles": {}, "last_scan": {"queued_count": 2}})
            os.utime(path, ns=(modified, modified))  # Same size and mtime; new inode.
            self.assertEqual(scanner.get_metrics_snapshot()['queued_count'], 2)
            path.unlink()
            self.assertEqual(scanner.get_metrics_snapshot()['queued_count'], 0)
