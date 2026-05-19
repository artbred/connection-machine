"""Custom exceptions for the LinkedIn automation tool."""

from datetime import datetime


class SessionExpiredException(Exception):
    """Raised when the LinkedIn session has expired."""

    pass


class TaskSkippedException(Exception):
    """Raised when a task is skipped (e.g. already pending, already connected).

    Should not count toward rate limits or scheduling delays.
    """

    def __init__(
        self,
        reason: str,
        *,
        cooldown_until: datetime | None = None,
        from_active_cooldown: bool = False,
    ):
        self.reason = reason
        self.cooldown_until = cooldown_until
        self.from_active_cooldown = from_active_cooldown
        super().__init__(f"Task skipped: {reason}")
