"""Custom exceptions for the LinkedIn automation tool."""


class SessionExpiredException(Exception):
    """Raised when the LinkedIn session has expired."""

    pass


class TaskSkippedException(Exception):
    """Raised when a task is skipped (e.g. already pending, already connected).
    
    Should not count toward rate limits or scheduling delays.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Task skipped: {reason}")
