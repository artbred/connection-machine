from __future__ import annotations

import os
import threading
import time
import logging

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


logger = logging.getLogger(__name__)


def _escape_label_value(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _format_sample(name: str, value: float | int, labels: dict[str, str] | None = None) -> str:
    if labels:
        label_text = ",".join(
            f'{key}="{_escape_label_value(str(val))}"'
            for key, val in sorted(labels.items())
        )
        return f"{name}{{{label_text}}} {value}"
    return f"{name} {value}"


class NoopMetrics:
    def shutdown(self):
        return None

    def set_up(self, value: bool):
        return None

    def set_linkedin_authenticated(self, value: bool):
        return None

    def inc_login_attempts(self):
        return None

    def inc_reauth(self):
        return None

    def mark_login_success(self):
        return None

    def mark_poll(self):
        return None

    def set_db_task_counts(self, counts: dict[tuple[str, str], int]):
        return None

    def set_next_execution_timestamps(self, timestamps: dict[str, float]):
        return None

    def set_autonomous_comment_allowed(self, value: bool):
        return None

    def observe_task(self, task_type: str, outcome: str, duration_seconds: float):
        return None

    def set_comment_history(
        self,
        total_comments: int,
        comments_by_day: dict[str, int],
        recent_entries: list[dict[str, str | float]],
    ):
        return None

    def set_invite_history(
        self,
        recent_entries: list[dict[str, str | float]],
    ):
        return None


class ConnectionMachineMetrics:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.started_at = time.time()
        self._lock = threading.RLock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

        self._up = 0
        self._linkedin_authenticated = 0
        self._login_attempts_total = 0
        self._reauth_total = 0
        self._last_login_timestamp = 0.0
        self._last_poll_timestamp = 0.0
        self._autonomous_comment_allowed = 0
        self._task_executions_total: dict[tuple[str, str], int] = {}
        self._task_duration_sum: dict[tuple[str, str], float] = {}
        self._task_duration_count: dict[tuple[str, str], int] = {}
        self._task_last_execution_timestamp: dict[tuple[str, str], float] = {}
        self._db_task_counts: dict[tuple[str, str], int] = {}
        self._next_execution_timestamps: dict[str, float] = {}
        self._comments_sent_total = 0
        self._comments_by_day: dict[str, int] = {}
        self._recent_comment_entries: list[dict[str, str | float]] = []
        self._recent_invite_entries: list[dict[str, str | float]] = []

    def start(self):
        metrics = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in ("/metrics", "/metrics/"):
                    self.send_response(404)
                    self.end_headers()
                    return

                payload = metrics.render().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):
                return None

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def shutdown(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    def set_up(self, value: bool):
        with self._lock:
            self._up = 1 if value else 0

    def set_linkedin_authenticated(self, value: bool):
        with self._lock:
            self._linkedin_authenticated = 1 if value else 0

    def inc_login_attempts(self):
        with self._lock:
            self._login_attempts_total += 1

    def inc_reauth(self):
        with self._lock:
            self._reauth_total += 1

    def mark_login_success(self):
        with self._lock:
            self._last_login_timestamp = time.time()

    def mark_poll(self):
        with self._lock:
            self._last_poll_timestamp = time.time()

    def set_db_task_counts(self, counts: dict[tuple[str, str], int]):
        with self._lock:
            self._db_task_counts = dict(counts)

    def set_next_execution_timestamps(self, timestamps: dict[str, float]):
        with self._lock:
            self._next_execution_timestamps = dict(timestamps)

    def set_autonomous_comment_allowed(self, value: bool):
        with self._lock:
            self._autonomous_comment_allowed = 1 if value else 0

    def observe_task(self, task_type: str, outcome: str, duration_seconds: float):
        key = (task_type, outcome)
        now = time.time()
        with self._lock:
            self._task_executions_total[key] = self._task_executions_total.get(key, 0) + 1
            self._task_duration_sum[key] = self._task_duration_sum.get(key, 0.0) + duration_seconds
            self._task_duration_count[key] = self._task_duration_count.get(key, 0) + 1
            self._task_last_execution_timestamp[key] = now

    def set_comment_history(
        self,
        total_comments: int,
        comments_by_day: dict[str, int],
        recent_entries: list[dict[str, str | float]],
    ):
        with self._lock:
            self._comments_sent_total = max(0, int(total_comments))
            self._comments_by_day = {
                str(day): max(0, int(count))
                for day, count in comments_by_day.items()
            }
            self._recent_comment_entries = [
                {
                    "author": str(entry.get("author") or ""),
                    "comment": str(entry.get("comment") or ""),
                    "commented_at": str(entry.get("commented_at_iso") or ""),
                    "commented_at_timestamp": float(
                        entry.get("commented_at_timestamp") or 0.0
                    ),
                    "post_href": str(entry.get("post_href") or ""),
                    "post_key": str(entry.get("post_key") or ""),
                }
                for entry in recent_entries
            ]

    def set_invite_history(
        self,
        recent_entries: list[dict[str, str | float]],
    ):
        with self._lock:
            self._recent_invite_entries = [
                {
                    "entry_key": str(entry.get("entry_key") or ""),
                    "message": str(entry.get("message") or ""),
                    "profile_url": str(entry.get("profile_url") or ""),
                    "sent_at": str(entry.get("sent_at_iso") or ""),
                    "sent_at_timestamp": float(entry.get("sent_at_timestamp") or 0.0),
                    "status": str(entry.get("status") or ""),
                }
                for entry in recent_entries
            ]

    def render(self) -> str:
        with self._lock:
            up = self._up
            linkedin_authenticated = self._linkedin_authenticated
            login_attempts_total = self._login_attempts_total
            reauth_total = self._reauth_total
            last_login_timestamp = self._last_login_timestamp
            last_poll_timestamp = self._last_poll_timestamp
            autonomous_comment_allowed = self._autonomous_comment_allowed
            task_executions_total = dict(self._task_executions_total)
            task_duration_sum = dict(self._task_duration_sum)
            task_duration_count = dict(self._task_duration_count)
            task_last_execution_timestamp = dict(self._task_last_execution_timestamp)
            db_task_counts = dict(self._db_task_counts)
            next_execution_timestamps = dict(self._next_execution_timestamps)
            comments_sent_total = self._comments_sent_total
            comments_by_day = dict(self._comments_by_day)
            recent_comment_entries = list(self._recent_comment_entries)
            recent_invite_entries = list(self._recent_invite_entries)

        lines = [
            "# HELP connection_machine_up Whether the connection-machine process considers itself healthy.",
            "# TYPE connection_machine_up gauge",
            _format_sample("connection_machine_up", up),
            "# HELP connection_machine_started_at_timestamp_seconds Unix timestamp when the process started.",
            "# TYPE connection_machine_started_at_timestamp_seconds gauge",
            _format_sample("connection_machine_started_at_timestamp_seconds", self.started_at),
            "# HELP connection_machine_linkedin_authenticated Whether LinkedIn auth is currently valid.",
            "# TYPE connection_machine_linkedin_authenticated gauge",
            _format_sample("connection_machine_linkedin_authenticated", linkedin_authenticated),
            "# HELP connection_machine_last_login_timestamp_seconds Unix timestamp of the last successful LinkedIn login.",
            "# TYPE connection_machine_last_login_timestamp_seconds gauge",
            _format_sample("connection_machine_last_login_timestamp_seconds", last_login_timestamp),
            "# HELP connection_machine_last_poll_timestamp_seconds Unix timestamp of the last dispatcher poll.",
            "# TYPE connection_machine_last_poll_timestamp_seconds gauge",
            _format_sample("connection_machine_last_poll_timestamp_seconds", last_poll_timestamp),
            "# HELP connection_machine_login_attempts_total Total LinkedIn login attempts.",
            "# TYPE connection_machine_login_attempts_total counter",
            _format_sample("connection_machine_login_attempts_total", login_attempts_total),
            "# HELP connection_machine_reauth_total Total LinkedIn re-authentication cycles.",
            "# TYPE connection_machine_reauth_total counter",
            _format_sample("connection_machine_reauth_total", reauth_total),
            "# HELP connection_machine_autonomous_comment_allowed Whether an autonomous feed comment action is currently allowed.",
            "# TYPE connection_machine_autonomous_comment_allowed gauge",
            _format_sample("connection_machine_autonomous_comment_allowed", autonomous_comment_allowed),
            "# HELP connection_machine_task_executions_total Total task executions partitioned by task type and outcome.",
            "# TYPE connection_machine_task_executions_total counter",
        ]

        for (task_type, outcome), value in sorted(task_executions_total.items()):
            lines.append(
                _format_sample(
                    "connection_machine_task_executions_total",
                    value,
                    {"task_type": task_type, "outcome": outcome},
                )
            )

        lines.extend(
            [
                "# HELP connection_machine_task_execution_duration_seconds_sum Total observed task execution time in seconds by task type and outcome.",
                "# TYPE connection_machine_task_execution_duration_seconds_sum counter",
            ]
        )
        for (task_type, outcome), value in sorted(task_duration_sum.items()):
            lines.append(
                _format_sample(
                    "connection_machine_task_execution_duration_seconds_sum",
                    value,
                    {"task_type": task_type, "outcome": outcome},
                )
            )

        lines.extend(
            [
                "# HELP connection_machine_task_execution_duration_seconds_count Number of observed task execution durations by task type and outcome.",
                "# TYPE connection_machine_task_execution_duration_seconds_count counter",
            ]
        )
        for (task_type, outcome), value in sorted(task_duration_count.items()):
            lines.append(
                _format_sample(
                    "connection_machine_task_execution_duration_seconds_count",
                    value,
                    {"task_type": task_type, "outcome": outcome},
                )
            )

        lines.extend(
            [
                "# HELP connection_machine_task_last_execution_timestamp_seconds Unix timestamp of the last task execution by task type and outcome.",
                "# TYPE connection_machine_task_last_execution_timestamp_seconds gauge",
            ]
        )
        for (task_type, outcome), value in sorted(task_last_execution_timestamp.items()):
            lines.append(
                _format_sample(
                    "connection_machine_task_last_execution_timestamp_seconds",
                    value,
                    {"task_type": task_type, "outcome": outcome},
                )
            )

        lines.extend(
            [
                "# HELP connection_machine_db_tasks Number of DB-backed tasks by task type and status.",
                "# TYPE connection_machine_db_tasks gauge",
            ]
        )
        for (task_type, status), value in sorted(db_task_counts.items()):
            lines.append(
                _format_sample(
                    "connection_machine_db_tasks",
                    value,
                    {"task_type": task_type, "status": status},
                )
            )

        lines.extend(
            [
                "# HELP connection_machine_next_execution_timestamp_seconds Unix timestamp when a task type can next run.",
                "# TYPE connection_machine_next_execution_timestamp_seconds gauge",
            ]
        )
        for task_type, value in sorted(next_execution_timestamps.items()):
            lines.append(
                _format_sample(
                    "connection_machine_next_execution_timestamp_seconds",
                    value,
                    {"task_type": task_type},
                )
            )

        lines.extend(
            [
                "# HELP connection_machine_comments_sent_total Total feed comments retained in local history.",
                "# TYPE connection_machine_comments_sent_total gauge",
                _format_sample(
                    "connection_machine_comments_sent_total",
                    comments_sent_total,
                ),
                "# HELP connection_machine_comments_sent_by_day Number of feed comments retained for each UTC day.",
                "# TYPE connection_machine_comments_sent_by_day gauge",
            ]
        )
        for day, value in sorted(comments_by_day.items()):
            lines.append(
                _format_sample(
                    "connection_machine_comments_sent_by_day",
                    value,
                    {"day": day},
                )
            )

        lines.extend(
            [
                "# HELP connection_machine_comment_history_entry_timestamp_seconds Unix timestamp for each recent feed comment entry retained for dashboard history.",
                "# TYPE connection_machine_comment_history_entry_timestamp_seconds gauge",
            ]
        )
        for entry in recent_comment_entries:
            lines.append(
                _format_sample(
                    "connection_machine_comment_history_entry_timestamp_seconds",
                    entry["commented_at_timestamp"],
                    {
                        "author": str(entry["author"]),
                        "comment": str(entry["comment"]),
                        "commented_at": str(entry["commented_at"]),
                        "post_href": str(entry["post_href"]),
                        "post_key": str(entry["post_key"]),
                    },
                )
            )

        lines.extend(
            [
                "# HELP connection_machine_invite_history_entry_timestamp_seconds Unix timestamp for each recent successful invite retained for dashboard history.",
                "# TYPE connection_machine_invite_history_entry_timestamp_seconds gauge",
            ]
        )
        for entry in recent_invite_entries:
            lines.append(
                _format_sample(
                    "connection_machine_invite_history_entry_timestamp_seconds",
                    entry["sent_at_timestamp"],
                    {
                        "entry_key": str(entry["entry_key"]),
                        "message": str(entry["message"]),
                        "profile_url": str(entry["profile_url"]),
                        "sent_at": str(entry["sent_at"]),
                        "status": str(entry["status"]),
                    },
                )
            )

        return "\n".join(lines) + "\n"


def create_metrics():
    enabled = os.getenv("METRICS_ENABLED", "true").lower() not in {"0", "false", "no"}
    if not enabled:
        return NoopMetrics()

    host = os.getenv("METRICS_HOST", "0.0.0.0")
    port = int(os.getenv("METRICS_PORT", "9102"))
    try:
        metrics = ConnectionMachineMetrics(host=host, port=port)
        metrics.start()
        return metrics
    except OSError as exc:
        logger.warning("Failed to start metrics server on %s:%s: %s", host, port, exc)
        return NoopMetrics()
