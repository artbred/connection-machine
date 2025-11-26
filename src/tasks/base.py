from abc import ABC, abstractmethod
import logging
from playwright.sync_api import Page
from human_actions import HumanActions

logger = logging.getLogger(__name__)

class BaseTask(ABC):
    def __init__(self, page):
        self.page: Page = page
        self.human = HumanActions(page)

    @abstractmethod
    def run(self, payload: dict):
        """
        Execute the task with the given payload.
        """
        pass

    def validate_session(self):
        """
        Check if the session is still valid.
        Raises SessionExpiredException if not.
        """
        # Simple check: look for login form
        # Note: This is a basic check. More robust checks might be needed depending on the page state.
        if self.page.locator("form.login__form").count() > 0:
            from exceptions import SessionExpiredException
            raise SessionExpiredException("Login form detected")
