"""Test package initialisation.

Blank the Telegram credentials before any test module (and therefore
``notifications``) is imported. ``.env`` carries live creds, and
``notifications.send_notification`` now reads them at call time, so without this
a test that exercises a notification path would post a real message to the
operator's chat. Runs first under ``python -m unittest discover``.
"""

import os

for _var in (
    "TELEGRAM_NOTIFICATIONS_URL",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_API_KEY",
):
    os.environ[_var] = ""
