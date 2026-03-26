import hashlib
import html
import json
import logging
import os
import random
import re

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .base import BaseTask
from exceptions import TaskSkippedException
from llm import generate_feed_comment
from notifications import send_notification

logger = logging.getLogger(__name__)

FEED_URL = "https://www.linkedin.com/feed/"
COMMENT_BUTTON_SELECTOR = "button:has-text('Comment')"
COMMENT_EDITOR_SELECTOR = "div[role='textbox'][aria-label='Text editor for creating comment']"
COMMENT_HISTORY_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "feed_comment_history.json"
)

MAX_SCROLL_ATTEMPTS = 4
MAX_CANDIDATES_PER_RUN = 15
MAX_POST_CONTENT_LENGTH = 3000
COMMENT_HISTORY_RETENTION_DAYS = 30
MIN_POST_CONTENT_LENGTH = 80

GENERIC_LINES = {
    "feed post",
    "suggested",
    "follow",
    "promoted",
    "like",
    "comment",
    "repost",
    "send",
    "reply",
    "load more comments",
    "start a post",
    "video",
    "photo",
    "write article",
}

ACTION_ROW_PATTERN = re.compile(r"^(like|comment|repost|send|reply)$", re.IGNORECASE)
METRIC_LINE_PATTERN = re.compile(
    r"^(?:\d[\d,\.]*\s+)+(?:reactions?|comments?|reposts?|followers?|follows?)\b",
    re.IGNORECASE,
)
TIMESTAMP_LINE_PATTERN = re.compile(
    r"^\d+\s*(?:s|sec|secs|m|min|mins|h|hr|hrs|d|day|days|w|wk|wks|mo|mos|y|yr|yrs)\b.*$",
    re.IGNORECASE,
)
PAGE_COUNTER_PATTERN = re.compile(r"^\d+\s*/\s*\d+$")

class FeedCommentTask(BaseTask):
    def run(self, payload: dict):
        if not os.getenv("OPENROUTER_API_KEY"):
            raise ValueError("OPENROUTER_API_KEY is required for feed comment task")

        self.validate_session()

        dry_run = bool(payload.get("dry_run", False))
        feed_url = payload.get("feed_url") or FEED_URL
        max_candidates = int(payload.get("max_candidates", MAX_CANDIDATES_PER_RUN))

        candidate_button, candidate = self._find_candidate(feed_url, max_candidates)
        if not candidate_button or not candidate:
            raise TaskSkippedException("no_safe_commentable_posts")

        logger.info(
            "Selected feed post %s by %s",
            candidate["post_key"],
            candidate.get("author") or "unknown author",
        )
        logger.info("Generated comment: %s", candidate["comment"])

        if dry_run:
            send_notification(
                self._build_notification_message(
                    candidate,
                    dry_run=True,
                )
            )
            return candidate

        self._post_comment(candidate_button, candidate["comment"])
        self._mark_post_commented(candidate)
        send_notification(
            self._build_notification_message(
                candidate,
                dry_run=False,
            )
        )
        return candidate

    def _find_candidate(
        self,
        feed_url: str,
        max_candidates: int,
    ) -> tuple[Any | None, dict[str, Any] | None]:
        history = self._load_comment_history()
        inspected_keys: set[str] = set()
        inspected_count = 0

        self.page.goto(feed_url, timeout=60000, wait_until="domcontentloaded")
        self.page.wait_for_selector("main", timeout=15000)
        self.human.random_sleep(4.0, 7.0)
        self.human.random_hover()

        for scroll_attempt in range(MAX_SCROLL_ATTEMPTS + 1):
            buttons = self.page.locator(COMMENT_BUTTON_SELECTOR)
            count = buttons.count()
            logger.info(
                "Feed comment scan %s/%s found %s visible comment buttons",
                scroll_attempt + 1,
                MAX_SCROLL_ATTEMPTS + 1,
                count,
            )

            for index in range(count):
                if inspected_count >= max_candidates:
                    return None, None

                button = buttons.nth(index)

                try:
                    if not button.is_visible(timeout=500):
                        continue
                except Exception:
                    continue

                candidate = self._extract_candidate(button)
                if not candidate:
                    continue

                post_key = candidate["post_key"]
                if post_key in inspected_keys:
                    continue

                inspected_keys.add(post_key)
                inspected_count += 1

                skip_reason = self._get_skip_reason(candidate, history)
                if skip_reason:
                    logger.info("Skipping post %s: %s", post_key, skip_reason)
                    continue

                decision = generate_feed_comment(candidate["post_content"])
                if not decision:
                    logger.warning("Comment generation failed for post %s", post_key)
                    continue

                if decision.get("isProhibit"):
                    logger.info(
                        "LLM prohibited commenting on post %s: %s",
                        post_key,
                        decision.get("reason", "unspecified"),
                    )
                    continue

                comment = (decision.get("comment") or "").strip()
                if not comment:
                    logger.info("LLM returned empty comment for post %s", post_key)
                    continue

                candidate["comment"] = comment
                candidate["decision_reason"] = decision.get("reason", "")
                return button, candidate

            if scroll_attempt == MAX_SCROLL_ATTEMPTS:
                break

            self._scroll_feed()

        return None, None

    def _extract_candidate(self, button) -> dict[str, Any] | None:
        data = button.evaluate(
            """
el => {
  let container = el;
  for (let i = 0; i < 12 && container; i++, container = container.parentElement) {
    const buttonTexts = Array.from(container.querySelectorAll('button'))
      .map(btn => (btn.innerText || '').trim())
      .filter(Boolean);
    const hasActionRow =
      buttonTexts.includes('Comment') &&
      (
        buttonTexts.includes('Repost') ||
        buttonTexts.includes('Send') ||
        buttonTexts.includes('Share')
      ) &&
      (buttonTexts.includes('Like') || buttonTexts.some(text => text.startsWith('Like')));
    const rawText = (container.innerText || '').trim();
    if (hasActionRow && rawText.length > 60) {
      const links = Array.from(container.querySelectorAll('a[href]'))
        .map(link => link.href)
        .filter(Boolean);
      const postHref = links.find(
        href =>
          href.includes('/feed/update/') ||
          href.includes('/posts/') ||
          href.includes('/activity-')
      ) || null;
      return {
        rawText,
        postHref,
      };
    }
  }
  return null;
}
"""
        )

        if not data:
            return None

        cleaned_lines = self._clean_post_lines(data["rawText"])
        if not cleaned_lines:
            return None

        post_content = "\n".join(cleaned_lines).strip()[:MAX_POST_CONTENT_LENGTH]
        if len(post_content) < MIN_POST_CONTENT_LENGTH:
            return None

        author = self._guess_author(cleaned_lines)
        post_key_source = data.get("postHref") or post_content[:500]
        post_key = hashlib.sha256(post_key_source.encode("utf-8")).hexdigest()[:16]

        return {
            "author": author,
            "post_content": post_content,
            "post_href": data.get("postHref"),
            "post_key": post_key,
        }

    def _clean_post_lines(self, raw_text: str) -> list[str]:
        cleaned_lines: list[str] = []
        for raw_line in raw_text.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line:
                continue

            normalized = line.lower()
            if normalized in GENERIC_LINES:
                continue
            if ACTION_ROW_PATTERN.match(line):
                continue
            if METRIC_LINE_PATTERN.match(line):
                continue
            if TIMESTAMP_LINE_PATTERN.match(line):
                continue
            if PAGE_COUNTER_PATTERN.match(line):
                continue
            if line.startswith("Select feed view:"):
                continue

            cleaned_lines.append(line)

        return cleaned_lines

    def _guess_author(self, lines: list[str]) -> str | None:
        for line in lines:
            normalized = line.lower()
            if normalized in GENERIC_LINES:
                continue
            if TIMESTAMP_LINE_PATTERN.match(line):
                continue
            if METRIC_LINE_PATTERN.match(line):
                continue
            return line[:120]
        return None

    def _get_skip_reason(
        self,
        candidate: dict[str, Any],
        history: dict[str, Any],
    ) -> str | None:
        if candidate["post_key"] in history:
            return "already_commented_recently"

        content = candidate["post_content"]
        lowered = content.lower()

        if "promoted" in lowered or "sponsored" in lowered:
            return "promoted_or_sponsored"

        return None

    def _post_comment(self, button, comment_text: str):
        button.scroll_into_view_if_needed()
        self.human.random_sleep(0.8, 1.6)
        self.human.click(button)

        editor = self.page.locator(COMMENT_EDITOR_SELECTOR).first
        editor.wait_for(state="visible", timeout=10000)
        self.human.type(editor, comment_text)
        self.human.random_sleep(0.8, 1.6)

        submit_button = button.locator(
            "xpath=ancestor::div[.//div[@role='textbox' and @aria-label='Text editor for creating comment']][1]//button[normalize-space()='Comment']"
        ).last
        submit_button.wait_for(state="visible", timeout=10000)
        self.human.click(submit_button)
        self.human.random_sleep(2.5, 4.5)

    def _scroll_feed(self):
        distance = random.randint(900, 1500)
        self.page.evaluate("(delta) => window.scrollBy(0, delta)", distance)
        self.human.random_sleep(2.0, 4.0)

    def _load_comment_history(self) -> dict[str, Any]:
        if not COMMENT_HISTORY_PATH.exists():
            return {}

        try:
            raw = json.loads(COMMENT_HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read feed comment history: %s", exc)
            return {}

        if not isinstance(raw, dict):
            return {}

        cutoff = datetime.utcnow() - timedelta(days=COMMENT_HISTORY_RETENTION_DAYS)
        pruned: dict[str, Any] = {}
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue

            commented_at = value.get("commented_at")
            if not commented_at:
                continue

            try:
                parsed = datetime.fromisoformat(commented_at)
            except ValueError:
                continue

            if parsed >= cutoff:
                pruned[key] = value

        return pruned

    def _mark_post_commented(self, candidate: dict[str, Any]):
        history = self._load_comment_history()
        history[candidate["post_key"]] = {
            "author": candidate.get("author"),
            "comment_preview": candidate["comment"][:120],
            "commented_at": datetime.utcnow().isoformat(),
            "post_href": candidate.get("post_href"),
        }

        COMMENT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        COMMENT_HISTORY_PATH.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _build_notification_message(
        self,
        candidate: dict[str, Any],
        dry_run: bool,
    ) -> str:
        status = "Dry Run Feed Comment" if dry_run else "Feed Comment Sent"
        author = html.escape(candidate.get("author") or "Unknown author")
        preview = html.escape(candidate["comment"][:120])
        post_href = candidate.get("post_href")

        lines = [f"<b>{status}</b>", f"Author: {author}"]
        if post_href:
            escaped_href = html.escape(post_href, quote=True)
            lines.append(f'Post: <a href="{escaped_href}">Open post</a>')
        else:
            lines.append("Post: unavailable")
        lines.append(f'Comment: "{preview}"')
        return "\n".join(lines)
