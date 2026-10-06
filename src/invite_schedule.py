"""Deterministic pacing from quota-consuming invite outcomes, using naive UTC."""

from collections.abc import Sequence
from datetime import datetime, timedelta


def next_invite_time(
    quota_events: Sequence[datetime], now: datetime, limit: int = 10
) -> datetime:
    """Return the earliest quota-aware start time for the next invite.

    The rolling window includes its exact 24-hour boundary. Confirmed and
    unconfirmed outcomes consume slots; duplicate/future timestamps are retained
    conservatively. Only the most recent ``limit`` events determine availability.
    No randomness or wall-clock reads are used, so persisted history reconstructs
    the same deadline after a restart.
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be a positive integer")
    if limit <= 0:
        raise ValueError("limit must be a positive integer")

    window = timedelta(hours=24)
    cutoff = now - window
    recent = sorted(timestamp for timestamp in quota_events if timestamp >= cutoff)[
        -limit:
    ]
    if not recent:
        return now

    slot = window / limit
    minimum_gap = slot * 0.7
    # Free capacity is usable once minimum spacing permits. Redistributing it
    # against the next-oldest event would postpone a slot as its predecessor ages out.
    quota_time = now
    if len(recent) == limit:
        quota_time = recent[0] + window + timedelta(microseconds=1)
    return max(now, recent[-1] + minimum_gap, quota_time)
