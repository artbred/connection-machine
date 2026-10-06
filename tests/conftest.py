"""Mirror the unittest bootstrap's outbound-service isolation for pytest.

Blank Telegram and OpenRouter credentials before imports can load ``.env``.
"""

import os

for _var in (
    "TELEGRAM_NOTIFICATIONS_URL",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_API_KEY",
    "OPENROUTER_API_KEY",
):
    os.environ[_var] = ""
