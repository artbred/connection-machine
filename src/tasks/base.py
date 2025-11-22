from abc import ABC, abstractmethod
import logging
from playwright.sync_api import Page

logger = logging.getLogger(__name__)

class BaseTask(ABC):
    def __init__(self, page):
        self.page: Page = page

    @abstractmethod
    def run(self, payload: dict):
        """
        Execute the task with the given payload.
        """
        pass
