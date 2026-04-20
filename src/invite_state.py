from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

INVITE_STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "invite_state.json"
INVITE_EVENT_RETENTION_DAYS = 30
INVITE_EVENT_HISTORY_LIMIT = 1000
INVITE_SKIP_COOLDOWNS = {
    "weekly_limit_reached": timedelta(days=7),
    "withdrawal_cooldown": timedelta(hours=6),
}


class InviteStateStore:
    def __init__(
        self,
        path: Path = INVITE_STATE_PATH,
        retention_days: int = INVITE_EVENT_RETENTION_DAYS,
    ):
        self.path = path
        self.retention_days = retention_days

    def _default_state(self) -> dict[str, Any]:
        return {
            "cooldown": None,
            "events": [],
        }

    def _cutoff(self, now: datetime | None = None) -> datetime:
        return (now or datetime.utcnow()) - timedelta(days=self.retention_days)

    def _parse_datetime(self, value: Any) -> datetime | None:
        if not value or not isinstance(value, str):
            return None

        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _normalize_event(self, raw: Any, cutoff: datetime) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None

        recorded_at = self._parse_datetime(raw.get("recorded_at"))
        if not recorded_at or recorded_at < cutoff:
            return None

        return {
            "event_id": str(raw.get("event_id") or ""),
            "recorded_at": recorded_at,
            "outcome": str(raw.get("outcome") or "unknown"),
            "reason": str(raw.get("reason") or ""),
            "profile_url": str(raw.get("profile_url") or ""),
            "message_preview": str(raw.get("message_preview") or ""),
            "status": str(raw.get("status") or ""),
            "source": str(raw.get("source") or ""),
            "cooldown_until": self._parse_datetime(raw.get("cooldown_until")),
        }

    def _normalize_cooldown(self, raw: Any, now: datetime) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None

        active_until = self._parse_datetime(raw.get("active_until"))
        if not active_until or active_until <= now:
            return None

        return {
            "reason": str(raw.get("reason") or ""),
            "active_until": active_until,
            "set_at": self._parse_datetime(raw.get("set_at")) or now,
            "source": str(raw.get("source") or ""),
            "profile_url": str(raw.get("profile_url") or ""),
        }

    def _serialize_state(self, state: dict[str, Any]) -> dict[str, Any]:
        cooldown = state.get("cooldown")
        serialized_events = []
        for event in state.get("events", []):
            serialized_events.append(
                {
                    "event_id": str(event.get("event_id") or ""),
                    "recorded_at": event["recorded_at"].isoformat(),
                    "outcome": str(event.get("outcome") or "unknown"),
                    "reason": str(event.get("reason") or ""),
                    "profile_url": str(event.get("profile_url") or ""),
                    "message_preview": str(event.get("message_preview") or ""),
                    "status": str(event.get("status") or ""),
                    "source": str(event.get("source") or ""),
                    "cooldown_until": (
                        event["cooldown_until"].isoformat()
                        if event.get("cooldown_until")
                        else None
                    ),
                }
            )

        return {
            "cooldown": (
                {
                    "reason": str(cooldown.get("reason") or ""),
                    "active_until": cooldown["active_until"].isoformat(),
                    "set_at": cooldown["set_at"].isoformat(),
                    "source": str(cooldown.get("source") or ""),
                    "profile_url": str(cooldown.get("profile_url") or ""),
                }
                if cooldown
                else None
            ),
            "events": serialized_events,
        }

    def load(self) -> dict[str, Any]:
        now = datetime.utcnow()
        state = self._default_state()

        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Failed to read invite state file: %s", exc)
                raw = None

            if isinstance(raw, dict):
                cutoff = self._cutoff(now)
                raw_events = raw.get("events", [])
                events = [
                    event
                    for event in (
                        self._normalize_event(item, cutoff)
                        for item in raw_events
                    )
                    if event is not None
                ]
                events.sort(key=lambda event: event["recorded_at"], reverse=True)
                state["events"] = events[:INVITE_EVENT_HISTORY_LIMIT]
                state["cooldown"] = self._normalize_cooldown(raw.get("cooldown"), now)
                should_save_pruned = (
                    len(state["events"]) != len(raw_events)
                    or (raw.get("cooldown") is not None and state["cooldown"] is None)
                )
                if should_save_pruned:
                    self.save(state)

        return state

    def save(self, state: dict[str, Any]) -> None:
        serialized = self._serialize_state(state)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(serialized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_active_cooldown(self, now: datetime | None = None) -> dict[str, Any] | None:
        state = self.load()
        cooldown = state.get("cooldown")
        current_time = now or datetime.utcnow()
        if not cooldown or cooldown["active_until"] <= current_time:
            if state.get("cooldown") is not None:
                state["cooldown"] = None
                self.save(state)
            return None
        return cooldown

    def set_cooldown(
        self,
        reason: str,
        active_until: datetime,
        *,
        source: str,
        profile_url: str = "",
    ) -> dict[str, Any]:
        state = self.load()
        existing = state.get("cooldown")
        if existing and existing["active_until"] > active_until and existing.get("reason") == reason:
            return existing

        cooldown = {
            "reason": reason,
            "active_until": active_until,
            "set_at": datetime.utcnow(),
            "source": source,
            "profile_url": profile_url,
        }
        state["cooldown"] = cooldown
        self.save(state)
        return cooldown

    def clear_cooldown(self) -> None:
        state = self.load()
        if state.get("cooldown") is None:
            return
        state["cooldown"] = None
        self.save(state)

    def record_event(
        self,
        *,
        outcome: str,
        reason: str = "",
        profile_url: str = "",
        message_preview: str = "",
        status: str = "",
        source: str,
        recorded_at: datetime | None = None,
        cooldown_until: datetime | None = None,
    ) -> dict[str, Any]:
        event_time = recorded_at or datetime.utcnow()
        normalized_message = (message_preview or "")[:200]
        event_id = hashlib.sha256(
            f"{event_time.isoformat()}|{outcome}|{reason}|{profile_url}|{status}".encode(
                "utf-8"
            )
        ).hexdigest()[:16]
        event = {
            "event_id": event_id,
            "recorded_at": event_time,
            "outcome": outcome,
            "reason": reason,
            "profile_url": profile_url,
            "message_preview": normalized_message,
            "status": status,
            "source": source,
            "cooldown_until": cooldown_until,
        }

        state = self.load()
        events = list(state.get("events", []))
        events.append(event)
        events.sort(key=lambda item: item["recorded_at"], reverse=True)
        state["events"] = events[:INVITE_EVENT_HISTORY_LIMIT]
        self.save(state)
        return event

    def get_recent_events(self, limit: int = 25) -> list[dict[str, Any]]:
        state = self.load()
        return list(state.get("events", []))[:limit]

    def get_event_counts(self) -> dict[tuple[str, str], int]:
        counts: dict[tuple[str, str], int] = {}
        for event in self.load().get("events", []):
            key = (event.get("outcome") or "unknown", event.get("reason") or "")
            counts[key] = counts.get(key, 0) + 1
        return counts

    def get_reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self.load().get("events", []):
            reason = event.get("reason") or ""
            if not reason:
                continue
            counts[reason] = counts.get(reason, 0) + 1
        return counts

    def get_last_event(self) -> dict[str, Any] | None:
        events = self.get_recent_events(limit=1)
        return events[0] if events else None

    def get_metrics_snapshot(self, recent_limit: int = 25) -> dict[str, Any]:
        return {
            "cooldown": self.get_active_cooldown(),
            "last_event": self.get_last_event(),
            "recent_events": self.get_recent_events(limit=recent_limit),
            "event_counts": self.get_event_counts(),
            "reason_counts": self.get_reason_counts(),
        }
