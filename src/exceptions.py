"""Custom exceptions for the LinkedIn automation tool."""

from datetime import datetime


class SessionExpiredException(Exception):
    """Raised when the LinkedIn session has expired."""

    pass


class TaskSkippedException(Exception):
    """Raised when a task is skipped (e.g. already pending, already connected).

    Most skips do not consume quota. Unconfirmed invitations are held conservatively.
    """

    def __init__(
        self,
        reason: str,
        *,
        cooldown_until: datetime | None = None,
        from_active_cooldown: bool = False,
        cooldown_eligible: bool = True,
        retryable_preflight: bool = False,
    ):
        self.reason = reason
        self.cooldown_until = cooldown_until
        self.from_active_cooldown = from_active_cooldown
        self.cooldown_eligible = cooldown_eligible
        self.retryable_preflight = retryable_preflight
        super().__init__(f"Task skipped: {reason}")
