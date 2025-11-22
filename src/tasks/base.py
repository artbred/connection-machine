from abc import ABC, abstractmethod
import logging

logger = logging.getLogger(__name__)

class BaseTask(ABC):
    def __init__(self, page):
        self.page = page

    @abstractmethod
    def run(self, payload: dict):
        """
        Execute the task with the given payload.
        """
        pass
