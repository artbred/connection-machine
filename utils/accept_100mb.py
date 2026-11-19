"""Run local offline fixtures, not authenticated parity, against a 100 MB ceiling.

Build locally: docker build -t connection-machine:100mb-check .
Run: python3 utils/accept_100mb.py [local-image]
One JSON record per fixture; a skipped or unmeasured fixture fails acceptance.
"""

import argparse
import json
import re
import signal
import subprocess
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CAP = 99_999_744  # Largest 4 KiB page multiple not exceeding 100 million bytes.
DEADLINE = 30
CASES = (
    "test_invite_schedule.InviteScheduleTests.test_full_quota_waits_for_oldest_success_to_leave_window",
    "test_db_migrations.DatabaseMigrationTests.test_legacy_schema_upgrade_twice_preserves_every_existing_value",
    "test_audience_filter.EnforceAudienceFilterTest.test_small_target_is_rejected_without_retry",
    "test_metrics.CommentHistoryMetricsTests.test_comment_history_entries_are_sorted_and_pruned",
    "test_invite_state.InviteStateStoreTests.test_repeated_weekly_feedback_creates_invite_cooldown",
    "test_dispatcher_scheduling.DispatcherSchedulingTests.test_only_due_invites_acquire_and_failed_login_leaves_them_pending",
    "test_dispatcher_scheduling.DispatcherSchedulingTests.test_interrupted_invites_are_held_not_replayed_on_restart",
    "test_dispatcher_scheduling.DispatcherSchedulingTests.test_preflight_retries_are_bounded_across_restarts",
    "test_dispatcher_scheduling.DispatcherSchedulingTests.test_background_scan_moves_into_idle_time_without_repeating_per_slot",
    "test_dispatcher_scheduling.DispatcherSchedulingTests.test_worker_stays_browser_free_from_start_during_durable_invite_limits",
    "test_dispatcher_scheduling.DispatcherSchedulingTests.test_worker_releases_each_browser_poll_even_on_reauth_failure",
    "test_notification_reply_invites.NotificationReplyInviteTests.test_queue_reply_invite_creates_prioritized_send_invite_task",
    "test_dry_run.DryRunTests.test_dry_run_rejects_send_options_before_startup",
    "test_dry_run.DryRunTests.test_logged_out_preview_neither_submits_credentials_nor_initializes_queue",
    "test_dry_run.DryRunTests.test_login_wall_and_unverified_feed_do_not_pass_preview",
    "test_browser_session.BrowserSessionTests.test_idle_close_and_restart_reuse_persistent_login",
    "test_invite_delivery.InviteDeliveryTest.test_complete_flow_personalizes_only_verified_owner_fields",
    "test_invite_delivery.InviteDeliveryTest.test_no_note_path_removes_preexisting_editor_text",
    "test_invite_delivery.InviteDeliveryTest.test_wrong_recipient_stops_before_typing",
    "test_invite_delivery.InviteDeliveryTest.test_unconfirmed_send_is_not_retried_or_accepted_from_generic_toast",
    "test_task_browser.TaskBrowserTests.test_post_composer_submits_requested_content",
    "test_task_browser.TaskBrowserTests.test_feed_dry_run_then_submit",
    "test_task_browser.TaskBrowserTests.test_notification_scan_selects_comment_engagement",
    "test_task_browser.TaskBrowserTests.test_post_dry_run_leaves_existing_draft_untouched",
    "test_task_browser.TaskBrowserTests.test_comment_dry_run_does_not_notify_or_store_history",
    "test_task_browser.TaskBrowserTests.test_notification_dry_run_never_opens_the_queue",
)

# Keep a small PID 1 alive to read the kernel counters after unittest/renderer exit.
# An independent GNU timeout bounds the fixture even if the host runner disappears.
RUN_ONE = r'''
set -eu
d=/sys/fs/cgroup
read -r cap < "$d/memory.max"
read -r swap < "$d/memory.swap.max"
printf 'LIMIT %s %s\n' "$cap" "$swap"
[ "$cap" -gt 0 ] && [ "$cap" -le 100000000 ] && [ "$swap" = 0 ] || exit 78
rc=0
LC_ALL=C timeout --verbose --kill-after=2 "$2" python -B -m unittest -v "$1" || rc=$?
read -r peak < "$d/memory.peak"
while read -r key n; do
    case "$key" in oom) oom=$n;; oom_kill) kills=$n;; esac
done < "$d/memory.events"
while read -r key n; do
    if [ "$key" = usage_usec ]; then cpu=$n; break; fi
done < "$d/cpu.stat"
printf 'USAGE %s %s %s %s\n' "$peak" "$oom" "$kills" "$cpu"
exit "$rc"
'''


def docker(*args, timeout=15):
    return subprocess.run(
        ["docker", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", timeout=timeout,
    )


def measure(image, case):
    name = "cm-100mb-" + uuid.uuid4().hex
    report = dict(test=case, memory_max=None, swap_max=None, memory_peak=None,
                  cpu_usec=None, oom=None, timeout=False, exit=None, passed=False)
    try:
        result = docker(
            "run", "--detach", "--pull=never", "--network=none", "--cgroupns=private",
            f"--memory={CAP}", f"--memory-swap={CAP}", "--name", name,
            "-v", f"{ROOT / 'tests'}:/app/tests:ro", "-w", "/app",
            "-e", "PYTHON_DOTENV_DISABLED=1", "-e", "DATABASE_URL=sqlite:///:memory:",
            "--entrypoint", "/bin/sh",
            image, "-c", RUN_ONE, "run-one", case, str(DEADLINE),
        )
        if result.returncode:
            raise RuntimeError(result.stdout.strip())
        try:
            docker("wait", name, timeout=DEADLINE + 12).check_returncode()
        except subprocess.TimeoutExpired:
            report["timeout"] = True
            docker("kill", name).check_returncode()
        state = docker("inspect", "--format", "{{json .State}}", name)
        state.check_returncode()
        state = json.loads(state.stdout)
        report["exit"] = state["ExitCode"]
        logs = docker("logs", name)
        logs.check_returncode()
        log = logs.stdout
        limit = re.search(r"^LIMIT (\d+) (\d+)$", log, re.M)
        if limit:
            report["memory_max"], report["swap_max"] = map(int, limit.groups())
        usage = re.search(r"^USAGE (\d+) (\d+) (\d+) (\d+)$", log, re.M)
        if usage:
            peak, oom, kills, cpu = map(int, usage.groups())
            report.update(memory_peak=peak, oom=bool(oom or kills), cpu_usec=cpu)
        if state["OOMKilled"]:
            report["oom"] = True
        report["timeout"] |= report["exit"] == 124 or "timeout: sending signal" in log
        # unittest returns zero for skips and expected failures; require an unqualified OK.
        report["passed"] = bool(
            report["exit"] == 0 and not report["timeout"] and report["oom"] is False
            and limit and 0 < report["memory_max"] <= 100_000_000 and report["swap_max"] == 0
            and 0 < report["memory_peak"] <= 100_000_000
            and re.search(r"^Ran 1 test in [^\n]+\n\nOK$", log, re.M)
        )
        if not report["passed"]:
            report["diagnostic"] = log[-3000:].strip()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
    finally:
        cleanup = docker("rm", "--force", name)
        if cleanup.returncode and "No such container" not in cleanup.stdout:
            raise RuntimeError(f"Cannot clean up {name}: {cleanup.stdout.strip()}")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", default="connection-machine:100mb-check")
    image = parser.parse_args().image
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    failures = 0
    for case in CASES:
        result = measure(image, "tests." + case)
        print(json.dumps(result), flush=True)
        failures += not result["passed"]
    sys.exit(bool(failures))
