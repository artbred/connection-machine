import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import llm  # noqa: E402
from tasks.invite import (  # noqa: E402
    InviteTask,
    MAX_PROFILE_CONTENT_LENGTH,
    MIN_PROFILE_CONTENT_LENGTH,
    PROFILE_CONTENT_EXTRACTION_JS,
    PROFILE_FOREIGN_MODULE_HEADINGS,
    sanitize_profile_content,
)


class SanitizeProfileContentTest(unittest.TestCase):
    def test_normalizes_name_whitespace(self):
        name, _ = sanitize_profile_content(
            "  Eric \n Melillo ", "x" * MIN_PROFILE_CONTENT_LENGTH
        )
        self.assertEqual(name, "Eric Melillo")

    def test_drops_content_below_minimum(self):
        name, content = sanitize_profile_content("Eric Melillo", "too thin")
        self.assertEqual(name, "Eric Melillo")
        self.assertEqual(content, "")

    def test_caps_content_at_maximum(self):
        _, content = sanitize_profile_content(
            "Eric", "y" * (MAX_PROFILE_CONTENT_LENGTH + 5000)
        )
        self.assertEqual(len(content), MAX_PROFILE_CONTENT_LENGTH)

    def test_handles_none_values(self):
        self.assertEqual(sanitize_profile_content(None, None), ("", ""))


class ForeignModuleBlocklistTest(unittest.TestCase):
    def test_observed_linkedin_modules_are_blocked(self):
        # Module headings observed live inside <main> on profile pages
        # (2026-07 SDUI renders). Removing any of these regresses the
        # wrong-person message bug.
        for heading in (
            "more profiles for you",
            "people also viewed",
            "explore premium profiles",
            "people you may know",
            "you might like",
            "pages for you",
        ):
            self.assertIn(heading, PROFILE_FOREIGN_MODULE_HEADINGS)

    def test_blocklist_is_lowercase(self):
        for heading in PROFILE_FOREIGN_MODULE_HEADINGS:
            self.assertEqual(heading, heading.lower())

    def test_extraction_js_hides_structural_noise(self):
        for selector in ("aside", "footer", '[role="dialog"]'):
            self.assertIn(selector, PROFILE_CONTENT_EXTRACTION_JS)


class GetProfileContentTest(unittest.TestCase):
    def _make_task(self, evaluate_result):
        class FakePage:
            def wait_for_selector(self, *args, **kwargs):
                return None

            def evaluate(self, script, *args):
                if "scrollTo" in script:
                    return None
                if "innerText.length" in script:
                    return 5000
                return evaluate_result

        class FakeHuman:
            def random_sleep(self, *args, **kwargs):
                return None

        task = InviteTask.__new__(InviteTask)
        task.page = FakePage()
        task.human = FakeHuman()
        return task

    def test_returns_name_and_content(self):
        task = self._make_task(
            {"name": "Eric Melillo", "content": "z" * MIN_PROFILE_CONTENT_LENGTH}
        )
        name, content = task.get_profile_content()
        self.assertEqual(name, "Eric Melillo")
        self.assertEqual(content, "z" * MIN_PROFILE_CONTENT_LENGTH)

    def test_returns_empty_when_extraction_returns_none(self):
        task = self._make_task(None)
        self.assertEqual(task.get_profile_content(), ("", ""))

    def test_returns_empty_content_when_too_thin(self):
        task = self._make_task({"name": "Eric Melillo", "content": "thin"})
        name, content = task.get_profile_content()
        self.assertEqual(name, "Eric Melillo")
        self.assertEqual(content, "")

    def test_returns_empty_on_page_error(self):
        task = self._make_task(None)

        def boom(*args, **kwargs):
            raise RuntimeError("page crashed")

        task.page.wait_for_selector = boom
        self.assertEqual(task.get_profile_content(), ("", ""))


class GenerateConnectionMessagePromptTest(unittest.TestCase):
    def _capture_prompt(self, profile_name):
        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "Hi there"}}]}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return FakeResponse()

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            with patch.object(llm.httpx, "post", side_effect=fake_post):
                message = llm.generate_connection_message(
                    "Profile body text", profile_name
                )

        self.assertEqual(message, "Hi there")
        return captured["payload"]["messages"][0]["content"]

    def test_prompt_is_anchored_to_prospect_name(self):
        prompt = self._capture_prompt("Eric Melillo")
        self.assertIn("Profile Content for Eric Melillo:", prompt)
        self.assertIn(
            "Never reference another person's name, headline, or achievements",
            prompt,
        )

    def test_prompt_falls_back_without_name(self):
        prompt = self._capture_prompt("")
        self.assertIn("Profile Content for the prospect:", prompt)


if __name__ == "__main__":
    unittest.main()
