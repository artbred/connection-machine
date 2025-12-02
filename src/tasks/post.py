import logging
from .base import BaseTask

logger = logging.getLogger(__name__)

class PostTask(BaseTask):
    def run(self, payload: dict):
        content = payload.get("content")
        if not content:
            raise ValueError("Content is required for post task")
        
