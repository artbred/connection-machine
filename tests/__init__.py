"""Blank outbound-service credentials before test modules load ``.env``.

Prevents real Telegram notifications and paid OpenRouter calls. Tests that
exercise generation supply a local transport or explicit mocked response.
Runs first under ``python -m unittest discover`` from the repository root.
"""

import os

for _var in (
    "TELEGRAM_NOTIFICATIONS_URL",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_API_KEY",
    "OPENROUTER_API_KEY",
):
    os.environ[_var] = ""
