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

MODULE_ENVS = (
    llm.CONNECTION_MESSAGE_MODEL_ENV,
    llm.REFINE_TEXT_MODEL_ENV,
    llm.FEED_COMMENT_MODEL_ENV,
    llm.CONNECT_ACTION_MODEL_ENV,
)

CLEARED = {name: "" for name in (llm.LLM_MODEL_ENV,) + MODULE_ENVS}


class ResolveModelTest(unittest.TestCase):
    def test_defaults_every_module_to_the_built_in_model(self):
        with patch.dict(os.environ, CLEARED):
            for env_name in MODULE_ENVS:
                self.assertEqual(llm.resolve_model(env_name), "google/gemini-3.7-flash")

    def test_llm_model_overrides_every_module(self):
        with patch.dict(os.environ, {**CLEARED, llm.LLM_MODEL_ENV: "vendor/other"}):
            for env_name in MODULE_ENVS:
                self.assertEqual(llm.resolve_model(env_name), "vendor/other")

    def test_per_module_override_wins_over_llm_model(self):
        env = {
            **CLEARED,
            llm.LLM_MODEL_ENV: "vendor/global",
            llm.CONNECT_ACTION_MODEL_ENV: "vendor/vision",
        }
        with patch.dict(os.environ, env):
            self.assertEqual(
                llm.resolve_model(llm.CONNECT_ACTION_MODEL_ENV), "vendor/vision"
            )
            self.assertEqual(
                llm.resolve_model(llm.FEED_COMMENT_MODEL_ENV), "vendor/global"
            )

    def test_blank_values_fall_through(self):
        env = {
            **CLEARED,
            llm.LLM_MODEL_ENV: "  vendor/global  ",
            llm.FEED_COMMENT_MODEL_ENV: "   ",
        }
        with patch.dict(os.environ, env):
            self.assertEqual(
                llm.resolve_model(llm.FEED_COMMENT_MODEL_ENV), "vendor/global"
            )


class RequestPayloadModelTest(unittest.TestCase):
    """Each call site must send the model its own env var resolves to."""

    def _capture_model(self, call, content="hi"):
        sent = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": content}}]}

        def fake_post(url, headers=None, json=None, timeout=None):
            sent["model"] = json["model"]
            return FakeResponse()

        with patch.object(llm.httpx, "post", side_effect=fake_post):
            call()
        self.assertIn("model", sent, "request was never sent")
        return sent["model"]

    def test_connection_message_uses_configured_model(self):
        env = {
            **CLEARED,
            "OPENROUTER_API_KEY": "test-key",
            llm.CONNECTION_MESSAGE_MODEL_ENV: "vendor/notes",
        }
        with patch.dict(os.environ, env):
            model = self._capture_model(
                lambda: llm.generate_connection_message("profile content", "Ann Lee")
            )
        self.assertEqual(model, "vendor/notes")

    def test_connection_message_defaults_to_built_in_model(self):
        with patch.dict(os.environ, {**CLEARED, "OPENROUTER_API_KEY": "test-key"}):
            model = self._capture_model(
                lambda: llm.generate_connection_message("profile content", "Ann Lee")
            )
        self.assertEqual(model, "google/gemini-3.7-flash")

    def test_refine_pass_uses_its_own_model(self):
        env = {**CLEARED, llm.REFINE_TEXT_MODEL_ENV: "vendor/refiner"}
        with patch.dict(os.environ, env):
            model = self._capture_model(
                lambda: llm._refine_text_length("x" * 500, 200, "test-key", "message")
            )
        self.assertEqual(model, "vendor/refiner")

    def test_feed_comment_uses_its_own_model(self):
        env = {
            **CLEARED,
            "OPENROUTER_API_KEY": "test-key",
            llm.LLM_MODEL_ENV: "vendor/global",
            llm.FEED_COMMENT_MODEL_ENV: "vendor/comments",
        }
        decision = '{"isProhibit": false, "reason": "safe", "comment": "nice"}'
        with patch.dict(os.environ, env):
            model = self._capture_model(
                lambda: llm.generate_feed_comment("post content"),
                content=decision,
            )
        self.assertEqual(model, "vendor/comments")

    def test_connect_action_uses_its_own_model(self):
        env = {
            **CLEARED,
            "OPENROUTER_API_KEY": "test-key",
            llm.LLM_MODEL_ENV: "vendor/global",
            llm.CONNECT_ACTION_MODEL_ENV: "vendor/vision",
        }
        action = '{"selector": "button", "expected_text": "Connect", "reason": "found"}'
        with patch.dict(os.environ, env):
            model = self._capture_model(
                lambda: llm.get_next_connect_action("Zm9v", "<button>Connect</button>"),
                content=action,
            )
        self.assertEqual(model, "vendor/vision")


if __name__ == "__main__":
    unittest.main()
