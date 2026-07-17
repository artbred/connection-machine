"""Pytest bootstrap: blank Telegram creds before any test import.

Mirror of ``tests/__init__.py`` for the pytest runner (pytest imports conftest
before collecting test modules). Keeps a stray real notification from being
sent when the suite runs with live creds in ``.env``.
"""

import os

for _var in (
    "TELEGRAM_NOTIFICATIONS_URL",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_API_KEY",
):
    os.environ[_var] = ""
