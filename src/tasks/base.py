from abc import ABC, abstractmethod
import logging
from playwright.sync_api import Page
from human_actions import HumanActions
from exceptions import SessionExpiredException

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
        current_url = self.page.url

        # Check for login-related URLs
        if "/login" in current_url or "/checkpoint" in current_url:
            raise SessionExpiredException(f"Redirected to auth page: {current_url}")

        # Check for login form
        if self.page.locator("form.login__form").count() > 0:
            raise SessionExpiredException("Login form detected")

        # Check for session expired modal/message
        if self.page.locator("text=session has expired").count() > 0:
            raise SessionExpiredException("Session expired message detected")

        # Check for sign-in button on page (indicates logged out)
        signin_buttons = self.page.locator("a[href*='/login'], a[data-tracking-control-name='auth_wall_']")
        if signin_buttons.count() > 0 and self.page.locator("nav").count() == 0:
            raise SessionExpiredException("Sign-in prompts detected without navigation")
