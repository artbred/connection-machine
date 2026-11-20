import re
from urllib.parse import urlsplit

from .base import BaseTask
from notifications import send_notification


class PostTask(BaseTask):
    def _open_composer(self, url=None):
        self.validate_session()
        if url is None:
            self.page.get_by_role("button", name="Start a post", exact=True).click(timeout=10000)
        else:
            self.page.goto(url, timeout=60000, wait_until="domcontentloaded")
        self.validate_session()
        post = self.page.get_by_role("button", name="Post", exact=True)
        classic = post.and_(self.page.locator("button.share-actions__primary-action"))
        modern = post.and_(self.page.locator("button[data-view-name='share-post']"))
        dialogs = self.page.locator("[role='dialog']:visible, dialog[open]:visible").filter(
            has=classic.or_(modern)
        )
        standalone = self.page.locator("form:visible:not([role='dialog'] *, dialog *)").filter(
            has=classic
        )
        composer = dialogs.or_(standalone)
        composer.wait_for(state="visible", timeout=10000)
        destination = urlsplit(self.page.url)
        if destination.scheme != "https" or destination.netloc not in {
            "linkedin.com", "www.linkedin.com",
        }:
            raise RuntimeError("Composer did not open on LinkedIn")
        scope = composer
        if composer.locator("button[data-view-name='share-post']:visible").count():
            scope = composer.locator("[data-testid='ui-core-tiptap-text-editor-wrapper']")
        editor = scope.locator("[role='textbox'][contenteditable='true']:visible")
        editor.wait_for(state="visible", timeout=20000)
        submit = composer.get_by_role("button", name="Post", exact=True)
        if submit.count() != 1 or not editor.is_editable():
            raise RuntimeError("Could not verify a unique editable post composer")
        return composer, editor, submit

    def inspect_composer(self):
        # Do not focus or fill the editor: LinkedIn can autosave without a publish click.
        self._open_composer()

    def run(self, payload: dict):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, expect

        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Content is required for post task")

        create_post_url = payload.get("create_post_url")
        if not create_post_url:
            raise ValueError("Create post URL is required for post task")

        composer, editor, submit = self._open_composer(create_post_url)
        editor.focus()
        # An enabled Post can also indicate a media-only draft.
        if editor.text_content() or submit.is_enabled():
            raise RuntimeError("Refusing to change an existing post draft")
        # Refuse a draft restored between focus and the fill action.
        editor.filter(has_not_text=re.compile(r"[\s\S]")).fill(content, timeout=5000)
        expect(submit).to_be_enabled(timeout=10000)
        if editor.inner_text() != content.replace("\r\n", "\n"):
            raise RuntimeError("Post text did not match the requested content")

        # Keep the original element: the Post button can disappear while sending.
        surface = composer.element_handle()
        if surface is None:
            raise RuntimeError("Post composer disappeared before submission")
        try:
            submit.click(timeout=5000)
            surface.wait_for_element_state("hidden", timeout=10000)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("Post submission not confirmed; not resending") from exc
        finally:
            surface.dispose()

        content_preview = f'"{content[:50]}..."' if len(content) > 50 else f'"{content}"'
        send_notification(f"Create Post\nContent: {content_preview}")

