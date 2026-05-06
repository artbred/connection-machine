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
COMMENT_BUTTON_SELECTOR = (
    "button:has-text('Comment'), "
    "button:has-text('Add a comment'), "
    "button[aria-label*='comment' i]"
)
COMMENT_EDITOR_SELECTOR = (
    "div[role='textbox'][aria-label='Text editor for creating comment']"
)
COMMENT_EDITOR_RELATIVE_XPATH = (
    "xpath=ancestor::*[.//div[@role='textbox' and "
    "@aria-label='Text editor for creating comment']][1]"
    "//div[@role='textbox' and @aria-label='Text editor for creating comment']"
)
COMMENT_HISTORY_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "feed_comment_history.json"
)

MAX_SCROLL_ATTEMPTS = 6
MAX_CANDIDATES_PER_RUN = 15
MAX_POST_CONTENT_LENGTH = 3000
COMMENT_HISTORY_RETENTION_DAYS = 30
MIN_POST_CONTENT_LENGTH = 80

GENERIC_LINES = {
    "feed post",
    "suggested",
    "follow",
    "following",
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
    "premium profile",
    "reposted this",
}

ACTION_ROW_PATTERN = re.compile(r"^(like|comment|repost|send|reply)$", re.IGNORECASE)
INTERACTION_PATTERN = re.compile(
    r"(?:commented|likes|celebrates|finds this|reposted|shared)\s+(?:on\s+)?this",
    re.IGNORECASE,
)
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

        _, candidate = self._find_candidate(feed_url, max_candidates)
        if not candidate:
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

        self._post_comment(candidate)
        # Try to extract a proper shareable link if current one is missing
        if not candidate.get("post_href"):
            share_link = self._extract_shareable_post_link(candidate)
            if share_link:
                candidate["post_href"] = share_link
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

        # Ensure we're on the main feed, not a single-post view
        self._ensure_main_feed(feed_url)

        # Debug: log page structure
        self._log_feed_debug()

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
                    logger.debug("No candidate extracted for button %s", index)
                    continue

                logger.debug(
                    "Candidate extracted: author=%s content_len=%s post_key=%s",
                    candidate.get("author"),
                    len(candidate.get("post_content", "")),
                    candidate["post_key"],
                )

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

            # If we've scrolled multiple times and only keep finding the same
            # already-commented post, try refreshing the feed to load new content
            if scroll_attempt >= 2 and inspected_count == 0 and len(inspected_keys) > 0:
                logger.info("Feed seems stuck on same post(s), refreshing page")
                self.page.goto(feed_url, timeout=60000, wait_until="domcontentloaded")
                self.page.wait_for_selector("main", timeout=15000)
                self.human.random_sleep(4.0, 7.0)
                self._ensure_main_feed(feed_url)
                self._log_feed_debug()
                continue

            self._scroll_feed()
            self._log_feed_debug()

        return None, None

    def _extract_candidate(self, button) -> dict[str, Any] | None:
        data = button.evaluate(
            """
(btn) => {
  // Walk up the DOM tree to find the post container.
  // LinkedIn feed posts are typically wrapped in <article> or <div> with
  // specific data-test-id or data-id attributes. We look for:
  // 1. An ancestor with data-test-id="feed-activity" or data-id containing "activity"
  // 2. OR an ancestor with substantial text (200-15000 chars)
  // 3. Must contain the button
  let container = null;
  let el = btn.parentElement;
  for (let level = 0; level < 25 && el; level++, el = el.parentElement) {
    if (el.tagName === 'BODY' || el.tagName === 'HTML') break;
    
    // Prefer LinkedIn's feed post containers by data attributes
    const dataTestId = el.getAttribute('data-test-id') || '';
    const dataId = el.getAttribute('data-id') || '';
    const dataUrn = el.getAttribute('data-urn') || '';
    if (dataTestId.includes('feed-activity') || 
        dataId.includes('activity') || 
        dataUrn.includes('activity') ||
        dataUrn.includes('urn:li:activity')) {
      container = el;
      break;
    }
    
    // Fallback: structural heuristic
    const textLen = (el.textContent || '').trim().length;
    if (textLen > 200 && textLen < 15000) {
      if (el.contains(btn)) {
        container = el;
        break;
      }
    }
  }

  if (!container) return null;

  const rawText = (container.textContent || '').trim();
  if (rawText.length < 80) return null;

  // Find post href - prioritize timestamp link (has <time> child)
  // then fall back to feed/update/activity patterns
  let postHref = null;
  const allLinks = Array.from(container.querySelectorAll('a[href]'))
    .map(a => a.href)
    .filter(Boolean);

  // Try 1: timestamp link is the most reliable permalink
  const timeAnchors = container.querySelectorAll('a:has(time)');
  for (const a of timeAnchors) {
    const h = a.href;
    if (h && (h.includes('/feed/update/') || h.includes('/activity-') || h.includes('/posts/') || h.includes('/ugcPost'))) {
      postHref = h;
      break;
    }
  }

  // Try 2: feed/update, activity, ugcPost patterns
  if (!postHref) {
    postHref = allLinks.find(h =>
      h.includes('/feed/update/') || h.includes('/activity-') || h.includes('/ugcPost')
    ) || null;
  }

  // Try 3: posts/ or feed/view patterns. Do not keep the generic feed URL;
  // it cannot be used to verify or revisit the specific post later.
  if (!postHref) {
    postHref = allLinks.find(h =>
      h.includes('/posts/') || h.includes('/feed/view/')
    ) || null;
  }

  return { rawText, postHref };
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

    def _find_button_for_post_key(self, post_key: str):
        buttons = self.page.locator(COMMENT_BUTTON_SELECTOR)
        count = buttons.count()
        for index in range(count):
            button = buttons.nth(index)
            try:
                if not button.is_visible(timeout=500):
                    continue
            except Exception:
                continue

            candidate = self._extract_candidate(button)
            if candidate and candidate["post_key"] == post_key:
                return button

        return None

    def _get_post_editor(self, button):
        scoped_editor = button.locator(COMMENT_EDITOR_RELATIVE_XPATH)
        try:
            if scoped_editor.count() > 0:
                return scoped_editor.first
        except Exception:
            pass

        # LinkedIn sometimes renders the editor as a sibling/cousin of the
        # Comment button. Fall back to the most recently visible editor.
        return self.page.locator(COMMENT_EDITOR_SELECTOR).last

    def _wait_for_post_editor(self, button, timeout: int = 5000):
        editor = self._get_post_editor(button)
        editor.wait_for(state="visible", timeout=timeout)
        return editor

    def _get_post_submit_button(self, button):
        editor = self._get_post_editor(button)
        try:
            marker = f"cm-submit-{random.randint(1_000_000, 9_999_999)}"
            selector = editor.evaluate(
                """
(editor, marker) => {
  for (let scope = editor; scope; scope = scope.parentElement) {
    const buttons = Array.from(scope.querySelectorAll('button'));
    for (const button of buttons) {
      const text = (button.textContent || '').trim().toLowerCase();
      const aria = (button.getAttribute('aria-label') || '').trim().toLowerCase();
      const label = `${text} ${aria}`;
      const rect = button.getBoundingClientRect();
      const visible = rect.width > 0 && rect.height > 0;
      const disabled = button.disabled || button.getAttribute('aria-disabled') === 'true';
      const isSubmit =
        button.type === 'submit' ||
        text === 'post' ||
        text === 'comment' ||
        label.includes('post comment') ||
        label.includes('submit comment');

      if (visible && !disabled && isSubmit) {
        button.setAttribute('data-connection-machine-submit', marker);
        return `[data-connection-machine-submit="${marker}"]`;
      }
    }
  }
  return null;
}
""",
                marker,
            )
            if selector:
                return self.page.locator(selector).first
        except Exception as exc:
            logger.debug("Could not resolve comment submit button near editor: %s", exc)

        fallback = self.page.locator(
            "button[type='submit'], "
            "button:has-text('Post'), "
            "button:has-text('Comment'), "
            "button[aria-label*='Post' i]"
        )
        for index in range(fallback.count() - 1, -1, -1):
            candidate = fallback.nth(index)
            if self._is_enabled_button(candidate):
                return candidate

        return fallback.last

    def _is_enabled_button(self, button) -> bool:
        try:
            if not button.is_visible(timeout=500):
                return False
            if button.is_disabled(timeout=500):
                return False
            aria_disabled = (button.get_attribute("aria-disabled") or "").lower()
            return aria_disabled != "true"
        except Exception:
            return False

    def _wait_for_comment_submission(self, editor, comment_text: str) -> bool:
        for _ in range(10):
            try:
                if not editor.is_visible(timeout=500):
                    return True

                editor_text = " ".join(editor.inner_text(timeout=500).split())
                if comment_text not in editor_text and len(editor_text) == 0:
                    return True
            except Exception:
                return True

            self.human.random_sleep(0.5, 0.9)

        return False

    def _clean_post_lines(self, raw_text: str) -> list[str]:
        cleaned_lines: list[str] = []
        for raw_line in raw_text.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line:
                continue

            normalized = line.lower()
            if normalized in GENERIC_LINES:
                continue
            if INTERACTION_PATTERN.search(line):
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
            if INTERACTION_PATTERN.search(line):
                continue
            if TIMESTAMP_LINE_PATTERN.match(line):
                continue
            if METRIC_LINE_PATTERN.match(line):
                continue

            # Strip common LinkedIn UI prefixes from the author line
            cleaned = line
            for prefix in [
                "Feed post",
                "Suggested",
                "Premium Profile",
                "Following",
                "Reposted this",
            ]:
                if cleaned.startswith(prefix):
                    cleaned = cleaned[len(prefix) :].strip()

            # Strip degree badges (1st, 2nd, 3rd+) from the name
            cleaned = re.sub(
                r"^(.*?)(?:\s+•\s+|\s+\d+(?:st|nd|rd|th)\+?\s+)", r"\1", cleaned
            )
            cleaned = re.sub(r"\s+\d+(?:st|nd|rd|th)\+?$", "", cleaned)

            # Take the name part before the first bullet separator
            for sep in [" • ", " · ", " | "]:
                if sep in cleaned:
                    return cleaned.split(sep)[0].strip()[:120]
            return cleaned.strip()[:120]
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

    def _post_comment(self, candidate: dict[str, Any]):
        button = self._find_button_for_post_key(candidate["post_key"])
        if not button:
            raise ValueError(
                f"Could not re-find comment button for post {candidate['post_key']}"
            )

        comment_text = candidate["comment"]
        button.scroll_into_view_if_needed()
        self.human.random_sleep(0.8, 1.6)
        self.human.click(button)

        try:
            editor = self._wait_for_post_editor(button, timeout=4000)
        except Exception:
            logger.warning(
                "Human click did not open comment editor for post %s, retrying with direct click",
                candidate["post_key"],
            )
            button.click(timeout=5000)
            editor = self._wait_for_post_editor(button, timeout=10000)

        self.human.type(editor, comment_text)
        self.human.random_sleep(0.8, 1.6)

        typed_text = " ".join(editor.inner_text(timeout=2000).split())
        if comment_text not in typed_text:
            raise ValueError("Comment text was not entered into the editor")

        submit_button = self._get_post_submit_button(button)
        if self._is_enabled_button(submit_button):
            try:
                submit_button.click(delay=100, timeout=5000)
                self.human.random_sleep(2.5, 4.5)
                if self._wait_for_comment_submission(editor, comment_text):
                    return
            except Exception as exc:
                logger.warning("Comment submit button click failed: %s", exc)

        for shortcut in ("Control+Enter", "Meta+Enter"):
            editor.click(timeout=5000)
            self.page.keyboard.press(shortcut)
            self.human.random_sleep(2.5, 4.5)
            if self._wait_for_comment_submission(editor, comment_text):
                return

        raise ValueError("Comment text remained in the editor after submit attempts")

    def _ensure_main_feed(self, feed_url: str):
        """Ensure we're viewing the main feed, not a single-post detail view."""
        # Check if URL indicates we're on a specific post rather than the feed
        current_url = self.page.url
        if (
            "/feed/update/" in current_url
            or "/posts/" in current_url
            or "/activity-" in current_url
        ):
            logger.info(
                "Detected single-post view at %s, navigating to main feed", current_url
            )
            self.page.goto(feed_url, timeout=60000, wait_until="domcontentloaded")
            self.page.wait_for_selector("main", timeout=15000)
            self.human.random_sleep(4.0, 7.0)
            return

        # Also check if we see a "Back to feed" or similar button
        try:
            back_button = self.page.locator(
                "button:has-text('Back to feed'), a:has-text('Back to feed'), [aria-label*='Back to feed']"
            ).first
            if back_button.is_visible(timeout=2000):
                logger.info("Clicking 'Back to feed' button")
                back_button.click()
                self.human.random_sleep(3.0, 5.0)
                return
        except Exception:
            pass

        # Try clicking the LinkedIn logo or Home nav to ensure fresh feed
        try:
            home_link = self.page.locator(
                "a[href='/feed/'], nav a:has-text('Home'), [aria-label='Home']"
            ).first
            if home_link.is_visible(timeout=2000):
                logger.info("Clicking Home link to ensure fresh feed")
                home_link.click()
                self.human.random_sleep(3.0, 5.0)
                self.page.wait_for_selector("main", timeout=15000)
                return
        except Exception:
            pass

    def _log_feed_debug(self):
        """Log debug info about the current feed page structure."""
        try:
            debug_info = self.page.evaluate("""
                () => {
                    const articles = document.querySelectorAll('article');
                    const feedItems = document.querySelectorAll('[data-test-id="feed-activity"], [data-id*="activity"], [class*="feed-shared"], [class*="update-v2"]');
                    const allButtons = Array.from(document.querySelectorAll('button'));
                    const commentButtons = allButtons.filter(b => {
                        const text = (b.textContent || '').trim().toLowerCase();
                        return text.includes('comment') || text.includes('reply') || b.getAttribute('aria-label')?.toLowerCase().includes('comment');
                    });
                    const visibleCommentButtons = commentButtons.filter(b => {
                        const rect = b.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0 && rect.top >= 0 && rect.top <= window.innerHeight;
                    });
                    return {
                        url: window.location.href,
                        articleCount: articles.length,
                        feedItemCount: feedItems.length,
                        totalButtonCount: allButtons.length,
                        commentButtonCount: commentButtons.length,
                        visibleCommentButtonCount: visibleCommentButtons.length,
                        commentButtonTexts: visibleCommentButtons.slice(0, 5).map(b => (b.textContent || '').trim().substring(0, 50)),
                        pageHeight: document.body.scrollHeight,
                        viewportHeight: window.innerHeight,
                        scrollY: window.scrollY,
                    };
                }
            """)
            logger.info(
                "Feed debug: url=%s articles=%s feedItems=%s totalButtons=%s commentButtons=%s visibleCommentButtons=%s pageHeight=%s viewport=%s scrollY=%s buttonTexts=%s",
                debug_info.get("url"),
                debug_info.get("articleCount"),
                debug_info.get("feedItemCount"),
                debug_info.get("totalButtonCount"),
                debug_info.get("commentButtonCount"),
                debug_info.get("visibleCommentButtonCount"),
                debug_info.get("pageHeight"),
                debug_info.get("viewportHeight"),
                debug_info.get("scrollY"),
                debug_info.get("commentButtonTexts"),
            )
        except Exception as exc:
            logger.warning("Feed debug failed: %s", exc)

    def _scroll_feed(self):
        # LinkedIn feed may use window scroll or an inner scrollable container.
        # Try both approaches to ensure new posts load.
        distance = random.randint(1200, 2000)

        # First try scrolling the window
        self.page.evaluate("(delta) => window.scrollBy(0, delta)", distance)
        self.human.random_sleep(2.0, 4.0)

        # Also try scrolling the main feed container if it exists
        self.page.evaluate(
            """
            (delta) => {
                let feed = document.querySelector('main.scaffold-layout__main');
                if (!feed) {
                    const el = document.querySelector('[data-test-id="feed-activity"]');
                    if (el) {
                        feed = el.closest('[class*="feed"], [class*="scaffold"], main, section');
                    }
                }
                if (!feed) {
                    feed = document.querySelector('main');
                }
                if (!feed) {
                    feed = document.body;
                }
                if (feed && feed.scrollHeight > feed.clientHeight) {
                    feed.scrollBy(0, delta);
                }
            }
        """,
            distance,
        )

        # Wait for LinkedIn to load more content
        self.human.random_sleep(3.0, 5.0)

        # Check if loading indicator is present and wait for it to disappear
        try:
            loading = self.page.locator(
                ".loading, [class*='loading'], [class*='spinner'], [aria-label*='loading']"
            ).first
            if loading.is_visible(timeout=2000):
                loading.wait_for(state="hidden", timeout=10000)
        except Exception:
            pass

        self.human.random_sleep(1.0, 2.0)

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

    def get_comment_timestamps(self) -> list[datetime]:
        timestamps: list[datetime] = []
        for value in self._load_comment_history().values():
            if not isinstance(value, dict):
                continue

            commented_at = value.get("commented_at")
            if not commented_at:
                continue

            try:
                timestamps.append(datetime.fromisoformat(commented_at))
            except ValueError:
                continue

        timestamps.sort()
        return timestamps

    def get_comment_history_entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for post_key, value in self._load_comment_history().items():
            if not isinstance(value, dict):
                continue

            commented_at = value.get("commented_at")
            if not commented_at:
                continue

            try:
                commented_at_dt = datetime.fromisoformat(commented_at)
            except ValueError:
                continue

            entries.append(
                {
                    "author": str(value.get("author") or ""),
                    "comment": str(
                        value.get("comment") or value.get("comment_preview") or ""
                    ),
                    "commented_at": commented_at_dt,
                    "post_href": str(value.get("post_href") or ""),
                    "post_key": post_key,
                }
            )

        entries.sort(key=lambda entry: entry["commented_at"], reverse=True)
        return entries

    def _mark_post_commented(self, candidate: dict[str, Any]):
        history = self._load_comment_history()
        history[candidate["post_key"]] = {
            "author": candidate.get("author"),
            "comment": candidate["comment"],
            "comment_preview": candidate["comment"][:120],
            "commented_at": datetime.utcnow().isoformat(),
            "post_href": candidate.get("post_href"),
        }

        COMMENT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        COMMENT_HISTORY_PATH.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _extract_shareable_post_link(self, candidate: dict[str, Any]) -> str | None:
        """Try to extract a proper shareable LinkedIn post link via the share button."""
        button = self._find_button_for_post_key(candidate["post_key"])
        if not button:
            logger.info("Could not re-find comment button for share link extraction")
            return None

        try:
            share_btn = self.page.locator(
                "button:has-text('Share'), button:has-text('Repost')"
            ).first
            if share_btn.is_visible(timeout=2000):
                share_btn.scroll_into_view_if_needed()
                self.human.random_sleep(0.5, 1.0)
                share_btn.click(timeout=3000)
                self.human.random_sleep(0.5, 1.0)

                # Look for a copy-link input or shareable URL in the dialog
                share_input = self.page.locator(
                    "input[readonly], input[aria-label*='link' i], input[aria-label*='URL' i], "
                    "input[aria-label*='url' i]"
                )
                share_input = share_input.first
                if share_input.is_visible(timeout=3000):
                    link = share_input.input_value(timeout=2000)
                    if link and (
                        link.startswith("http://") or link.startswith("https://")
                    ):
                        logger.info("Extracted shareable link: %s", link)
                        self._dismiss_share_dialog(button)
                        return link

                # Alternative: click "Copy link to post" text
                copy_link = self.page.locator(
                    "text=Copy link, text=Copy link to post, text=Copy, "
                    "[data-testid*='copy-link'], [aria-label*='copy' i]"
                )
                copy_link = copy_link.first
                if copy_link.is_visible(timeout=2000):
                    copy_link.click(timeout=2000)
                    self.human.random_sleep(0.5, 1.0)

                self._dismiss_share_dialog(button)
        except Exception as exc:
            logger.warning("Failed to extract shareable post link: %s", exc)
            try:
                self._dismiss_share_dialog(button)
            except Exception:
                pass

        return None

    def _dismiss_share_dialog(self, button) -> None:
        """Close any open share dialog."""
        for close_sel in [
            "button[aria-label='Dismiss']",
            "button[aria-label='Close']",
            ".artdeco-modal__dismiss",
        ]:
            try:
                cb = self.page.locator(close_sel).first
                if cb.is_visible(timeout=500):
                    cb.click(timeout=1000)
            except Exception:
                pass

    def _build_notification_message(
        self,
        candidate: dict[str, Any],
        dry_run: bool,
    ) -> str:
        status = "Dry Run Feed Comment" if dry_run else "Feed Comment Sent"
        author = html.escape(candidate.get("author") or "Unknown author")
        comment_str = html.escape(candidate["comment"])
        post_href = candidate.get("post_href")

        # Post content summary: skip the author line, take next meaningful lines
        post_content = candidate.get("post_content", "")
        content_lines = post_content.splitlines()
        # Skip first line if it looks like the author/header line
        summary_lines = []
        for line in content_lines[1:]:
            normalized = line.lower().strip()
            if normalized in GENERIC_LINES:
                continue
            if INTERACTION_PATTERN.search(line):
                continue
            if TIMESTAMP_LINE_PATTERN.match(line):
                continue
            if METRIC_LINE_PATTERN.match(line):
                continue
            if not normalized:
                continue
            summary_lines.append(line)
            if len("\n".join(summary_lines)) >= 250:
                break

        post_summary = html.escape("\n".join(summary_lines)[:250])
        if len("\n".join(summary_lines)) > 250:
            post_summary += "..."

        lines = [f"<b>{status}</b>", f"Author: {author}"]
        if post_href:
            escaped_href = html.escape(post_href, quote=True)
            lines.append(f"Link: <a href='{escaped_href}'>Open post</a>")
        else:
            lines.append("Post: No link available")
        if post_summary:
            lines.append(f"Post summary: {post_summary}")
        lines.append(f'Comment: "{comment_str}"')
        return "\n".join(lines)
