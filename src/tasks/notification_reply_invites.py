import hashlib
import json
import logging
import re

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from sqlalchemy.orm import Session

from .base import BaseTask
from connection_state import ConnectionState, detect_connection_state
from db import Task, TaskStatus, TaskType

logger = logging.getLogger(__name__)

NOTIFICATIONS_URL = "https://www.linkedin.com/notifications/?filter=all"
NOTIFICATION_REPLY_INVITE_SOURCE = "notification_reply"
NOTIFICATION_REPLY_STATE_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "notification_reply_invites.json"
)
MAX_NOTIFICATION_CANDIDATES = 20
MAX_NOTIFICATION_QUEUE_PER_SCAN = 5
MAX_NOTIFICATION_SCROLL_ATTEMPTS = 8
MAX_LATEST_COMMENT_ENGAGEMENTS = 10
PRIORITY_CREATED_AT_OFFSET = timedelta(days=3650)
COMMENT_ENGAGEMENT_RE = re.compile(
    r"\b(?:replied|responded|commented|liked|loved|celebrated|supported|reposted|shared|reacted)\b"
    r".*\byour\s+comments?\b|\byour\s+comments?\b"
    r".*\b(?:reply|replies|replied|like|liked|love|loved|repost|reposted|commented|shared|reacted)\b",
    re.IGNORECASE,
)


def normalize_profile_url(url: str) -> str:
    parsed = urlparse(urljoin("https://www.linkedin.com", url.strip()))
    path = parsed.path.rstrip("/")
    if not path.startswith("/in/"):
        return ""
    return f"https://www.linkedin.com{path}/"


def notification_key(profile_url: str, text: str) -> str:
    stable_text = re.sub(
        r"\bstatus\s+is\s+(?:online|offline)\b",
        "",
        " ".join(text.lower().split()),
    ).strip()
    return hashlib.sha256(
        f"{profile_url}|{stable_text[:300]}".encode("utf-8")
    ).hexdigest()[:16]


def is_comment_engagement_notification(text: str) -> bool:
    return bool(COMMENT_ENGAGEMENT_RE.search(" ".join(text.split())))


class NotificationReplyInviteScanner(BaseTask):
    def __init__(self, page, state_path: Path = NOTIFICATION_REPLY_STATE_PATH):
        super().__init__(page)
        self.state_path = state_path

    def run(self, payload: dict) -> list[dict[str, Any]]:
        max_candidates = int(
            payload.get("max_candidates", MAX_LATEST_COMMENT_ENGAGEMENTS)
        )
        max_to_queue = int(payload.get("max_to_queue", MAX_NOTIFICATION_QUEUE_PER_SCAN))

        from db import SessionLocal

        with SessionLocal() as db:
            return self.queue_reply_invites(db, max_candidates, max_to_queue)

    def queue_reply_invites(
        self,
        db: Session,
        max_candidates: int = MAX_LATEST_COMMENT_ENGAGEMENTS,
        max_to_queue: int = MAX_NOTIFICATION_QUEUE_PER_SCAN,
    ) -> list[dict[str, Any]]:
        self.validate_session()
        candidates = self.find_reply_candidates(max_candidates)
        scan_started_at = datetime.utcnow()
        state = self._load_state()
        if not candidates:
            logger.info("No notification reply invite candidates found")
            self._record_scan_summary(state, scan_started_at, 0, 0, 0)
            self._save_state(state)
            return []

        seen_notifications = state.setdefault("seen_notifications", {})
        queued: list[dict[str, Any]] = []
        seen_count = 0
        priority_created_at = datetime.utcnow() - PRIORITY_CREATED_AT_OFFSET

        for candidate in candidates:
            if len(queued) >= max_to_queue:
                break

            candidate_key = candidate.get("notification_key") or notification_key(
                candidate.get("profile_url") or "",
                candidate.get("text") or "",
            )
            if candidate_key in seen_notifications:
                seen_count += 1
                continue

            profile_url = candidate["profile_url"]
            normalized_url = normalize_profile_url(profile_url)
            if not normalized_url:
                continue

            existing_task = self._find_existing_invite_task(db, normalized_url)
            profiles = state.setdefault("profiles", {})
            existing_entry = profiles.get(normalized_url)
            existing_status = existing_entry.get("status") if existing_entry else ""
            retry_failed_task = (
                existing_status == "queued"
                and existing_task is not None
                and existing_task.status == TaskStatus.FAILED
            )
            if (
                existing_entry
                and existing_status
                in {
                    "queued",
                    "already_connected",
                    "already_pending",
                }
                and not retry_failed_task
            ):
                logger.info(
                    "Skipping notification reply candidate already handled: %s",
                    normalized_url,
                )
                self._record_state(
                    state,
                    normalized_url,
                    candidate,
                    existing_status,
                    task_id=existing_entry.get("task_id"),
                )
                continue

            if existing_task:
                if existing_task.status in {TaskStatus.PENDING, TaskStatus.FAILED}:
                    self._prioritize_existing_task(
                        existing_task,
                        candidate,
                        priority_created_at,
                    )
                    existing_task.status = TaskStatus.PENDING
                    existing_task.error = None
                    db.commit()
                    record = self._record_state(
                        state,
                        normalized_url,
                        candidate,
                        "queued",
                        task_id=existing_task.id,
                    )
                    queued.append(record)
                else:
                    self._record_state(
                        state,
                        normalized_url,
                        candidate,
                        f"existing_{existing_task.status.value}",
                        task_id=existing_task.id,
                    )
                continue

            connection_state = self._get_connection_state(normalized_url)
            if connection_state == ConnectionState.CONNECTED:
                self._record_state(
                    state, normalized_url, candidate, "already_connected"
                )
                continue
            if connection_state == ConnectionState.PENDING:
                self._record_state(state, normalized_url, candidate, "already_pending")
                continue

            task = Task(
                type=TaskType.SEND_INVITE,
                payload=json.dumps(
                    {
                        "url": normalized_url,
                        "try_personal_message": True,
                        "source": NOTIFICATION_REPLY_INVITE_SOURCE,
                        "notification_profile_name": candidate.get("name") or "",
                        "notification_text": candidate.get("text") or "",
                    }
                ),
                status=TaskStatus.PENDING,
                created_at=priority_created_at,
            )
            db.add(task)
            db.commit()
            db.refresh(task)
            record = self._record_state(
                state,
                normalized_url,
                candidate,
                "queued",
                task_id=task.id,
            )
            queued.append(record)

        self._record_scan_summary(
            state,
            scan_started_at,
            len(candidates),
            seen_count,
            len(queued),
        )
        self._save_state(state)
        if queued:
            logger.info("Queued %s notification reply invite candidate(s)", len(queued))
        elif seen_count == len(candidates):
            logger.info(
                "Latest %s notification comment engagement(s) were already seen",
                len(candidates),
            )
        return queued

    def find_reply_candidates(
        self,
        max_candidates: int = MAX_LATEST_COMMENT_ENGAGEMENTS,
    ) -> list[dict[str, Any]]:
        self.page.goto(NOTIFICATIONS_URL, timeout=60000, wait_until="domcontentloaded")
        self.page.wait_for_selector("body", timeout=15000)
        self.human.random_sleep(2.0, 4.0)
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        target_count = min(max_candidates, MAX_LATEST_COMMENT_ENGAGEMENTS)
        for scroll_attempt in range(MAX_NOTIFICATION_SCROLL_ATTEMPTS + 1):
            for candidate in self._extract_reply_candidates(max_candidates):
                candidate_key = candidate["notification_key"]
                if candidate_key in seen:
                    continue
                candidates.append(candidate)
                seen.add(candidate_key)
                if len(candidates) >= target_count:
                    logger.info(
                        "Found %s notification reply candidate(s)", len(candidates)
                    )
                    return candidates

            if scroll_attempt == MAX_NOTIFICATION_SCROLL_ATTEMPTS:
                break

            self.page.evaluate(
                "window.scrollBy(0, Math.floor(window.innerHeight * 0.8))"
            )
            self.human.random_sleep(1.0, 2.0)

        logger.info("Found %s notification reply candidate(s)", len(candidates))
        return candidates

    def _extract_reply_candidates(
        self,
        max_candidates: int = MAX_LATEST_COMMENT_ENGAGEMENTS,
    ) -> list[dict[str, Any]]:
        raw_candidates = self.page.evaluate(
            """
({ maxCandidates }) => {
  const containers = Array.from(document.querySelectorAll('li, article, div[role="listitem"], div[data-urn], div.nt-card'));
  const candidates = [];

  for (const container of containers) {
    const text = (container.textContent || '').replace(/\s+/g, ' ').trim();
    if (!text || text.length < 20 || text.length > 2000) continue;

    const links = Array.from(container.querySelectorAll('a[href]'));
    const profileLinks = links.filter(link => {
      const href = link.getAttribute('href') || '';
      return href.includes('/in/') && !href.includes('/company/');
    });
    const profileLink = profileLinks.find(link => {
      const label = (link.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
      return label && !label.startsWith('status is ');
    }) || profileLinks[0];
    if (!profileLink) continue;

    const profileUrl = new URL(profileLink.getAttribute('href'), 'https://www.linkedin.com').href.split('?')[0].replace(/\/$/, '/') ;
    const name = (profileLink.textContent || '').replace(/\s+/g, ' ').trim();
    candidates.push({ profile_url: profileUrl, name, text });
    if (candidates.length >= Math.max(maxCandidates * 4, 40)) break;
  }

  return candidates;
}
""",
            {"maxCandidates": max_candidates},
        )

        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        if not isinstance(raw_candidates, list):
            return candidates

        for raw_candidate in raw_candidates:
            if not isinstance(raw_candidate, dict):
                continue
            profile_url = normalize_profile_url(
                str(raw_candidate.get("profile_url") or "")
            )
            if not profile_url or profile_url in seen:
                continue
            text = " ".join(str(raw_candidate.get("text") or "").split())
            if not is_comment_engagement_notification(text):
                continue
            name = " ".join(str(raw_candidate.get("name") or "").split())
            candidates.append(
                {
                    "profile_url": profile_url,
                    "name": name,
                    "text": text[:500],
                    "notification_key": notification_key(profile_url, text),
                }
            )
            seen.add(profile_url)

        return candidates

    def _get_connection_state(self, profile_url: str) -> ConnectionState:
        self.page.goto(profile_url, timeout=60000, wait_until="domcontentloaded")
        self.page.wait_for_selector("main", timeout=15000)
        self.human.random_sleep(1.0, 2.0)
        return detect_connection_state(self.page)

    def _find_existing_invite_task(
        self,
        db: Session,
        normalized_url: str,
    ) -> Task | None:
        tasks = db.query(Task).filter(Task.type == TaskType.SEND_INVITE).all()
        for task in tasks:
            try:
                payload = json.loads(task.payload)
            except (TypeError, json.JSONDecodeError):
                continue
            if normalize_profile_url(str(payload.get("url") or "")) == normalized_url:
                return task
        return None

    def _prioritize_existing_task(
        self,
        task: Task,
        candidate: dict[str, Any],
        priority_created_at: datetime,
    ) -> None:
        try:
            payload = json.loads(task.payload)
        except (TypeError, json.JSONDecodeError):
            payload = {}
        payload.update(
            {
                "source": NOTIFICATION_REPLY_INVITE_SOURCE,
                "notification_profile_name": candidate.get("name") or "",
                "notification_text": candidate.get("text") or "",
            }
        )
        task.payload = json.dumps(payload)
        task.created_at = priority_created_at

    def _load_state(self) -> dict[str, Any]:
        default_state: dict[str, Any] = {
            "profiles": {},
            "seen_notifications": {},
            "last_scan": {},
        }
        if not self.state_path.exists():
            return default_state
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read notification reply invite state: %s", exc)
            return default_state
        if not isinstance(raw, dict):
            return default_state

        if "profiles" in raw or "seen_notifications" in raw:
            raw.setdefault("profiles", {})
            seen_notifications = raw.setdefault("seen_notifications", {})
            profiles = raw.get("profiles")
            if isinstance(profiles, dict):
                for record in profiles.values():
                    if not isinstance(record, dict):
                        continue
                    normalized_key = notification_key(
                        str(record.get("profile_url") or ""),
                        str(record.get("notification_text") or ""),
                    )
                    if normalized_key:
                        seen_notifications.setdefault(normalized_key, record)
            raw.setdefault("last_scan", {})
            return raw

        profiles = raw
        seen_notifications = {}
        for record in profiles.values():
            if not isinstance(record, dict):
                continue
            key = record.get("notification_key")
            if key:
                seen_notifications[str(key)] = record
            normalized_key = notification_key(
                str(record.get("profile_url") or ""),
                str(record.get("notification_text") or ""),
            )
            if normalized_key:
                seen_notifications[normalized_key] = record
        return {
            "profiles": profiles,
            "seen_notifications": seen_notifications,
            "last_scan": {},
        }

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _record_state(
        self,
        state: dict[str, Any],
        profile_url: str,
        candidate: dict[str, Any],
        status: str,
        *,
        task_id: int | None = None,
    ) -> dict[str, Any]:
        record = {
            "profile_url": profile_url,
            "name": candidate.get("name") or "",
            "notification_key": candidate.get("notification_key") or "",
            "notification_text": candidate.get("text") or "",
            "status": status,
            "task_id": task_id,
            "updated_at": datetime.utcnow().isoformat(),
        }
        state.setdefault("profiles", {})[profile_url] = record
        notification_id = record.get("notification_key")
        if notification_id:
            state.setdefault("seen_notifications", {})[notification_id] = record
        return record

    def _record_scan_summary(
        self,
        state: dict[str, Any],
        scanned_at: datetime,
        latest_engagement_count: int,
        seen_count: int,
        queued_count: int,
    ) -> None:
        state["last_scan"] = {
            "scanned_at": scanned_at.isoformat(),
            "latest_engagement_count": latest_engagement_count,
            "seen_count": seen_count,
            "queued_count": queued_count,
        }

    def mark_task_status(self, task_id: int, status: str) -> None:
        state = self._load_state()
        profiles = state.get("profiles")
        seen_notifications = state.get("seen_notifications")
        if not isinstance(profiles, dict) or not isinstance(seen_notifications, dict):
            return

        updated = False
        for record in profiles.values():
            if not isinstance(record, dict) or record.get("task_id") != task_id:
                continue
            record["status"] = status
            record["updated_at"] = datetime.utcnow().isoformat()
            notification_id = record.get("notification_key")
            if notification_id:
                seen_notifications[str(notification_id)] = record
            updated = True

        if updated:
            self._save_state(state)

    def get_metrics_snapshot(self) -> dict[str, Any]:
        state = self._load_state()
        last_scan = state.get("last_scan") if isinstance(state, dict) else {}
        if not isinstance(last_scan, dict):
            last_scan = {}

        scanned_at = None
        try:
            scanned_at = datetime.fromisoformat(str(last_scan.get("scanned_at")))
        except ValueError:
            pass

        return {
            "last_scan_timestamp": scanned_at.timestamp() if scanned_at else 0.0,
            "latest_engagement_count": int(
                last_scan.get("latest_engagement_count") or 0
            ),
            "seen_count": int(last_scan.get("seen_count") or 0),
            "queued_count": int(last_scan.get("queued_count") or 0),
        }
