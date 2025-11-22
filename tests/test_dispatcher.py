import sys
import os
import json
import logging
from datetime import datetime, timedelta
from unittest.mock import MagicMock

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), "../src"))

from db import init_db, SessionLocal, Task, TaskType, TaskStatus, Base, engine
from dispatcher import TaskDispatcher

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_dispatcher():
    # Drop all tables to ensure clean state for new schema
    Base.metadata.drop_all(bind=engine)
    
    # Initialize DB
    init_db()
    
    # Mock Page object
    mock_page = MagicMock()
    
    # Initialize Dispatcher
    dispatcher = TaskDispatcher(mock_page)
    
    # Clear existing tasks for testing
    with SessionLocal() as db:
        db.query(Task).delete()
        db.commit()
        
    # Test 1: Create and execute a task
    logger.info("Test 1: Create and execute a task")
    with SessionLocal() as db:
        task = Task(
            type=TaskType.CREATE_POST,
            payload=json.dumps({"content": "Hello World"}),
            status=TaskStatus.PENDING
        )
        db.add(task)
        db.commit()
        task_id = task.id
        
    dispatcher.poll()
    
    with SessionLocal() as db:
        task = db.query(Task).filter(Task.id == task_id).first()
        assert task.status == TaskStatus.COMPLETED
        assert task.executed_at is not None
        logger.info("Task executed successfully")

    # Test 2: Rate Limiting
    logger.info("Test 2: Rate Limiting")
    # Insert 50 completed tasks for create_post (limit is 50)
    # We need to manually insert them as "completed" with executed_at
    with SessionLocal() as db:
        for _ in range(50):
            task = Task(
                type=TaskType.CREATE_POST,
                payload=json.dumps({"content": "Old task"}),
                status=TaskStatus.COMPLETED,
                executed_at=datetime.utcnow()
            )
            db.add(task)
        db.commit()
        
    # Create another task
    with SessionLocal() as db:
        task = Task(
            type=TaskType.CREATE_POST,
            payload=json.dumps({"content": "Should be rate limited"}),
            status=TaskStatus.PENDING
        )
        db.add(task)
        db.commit()
        task_id = task.id
        
    dispatcher.poll()
    
    with SessionLocal() as db:
        task = db.query(Task).filter(Task.id == task_id).first()
        assert task.status == TaskStatus.PENDING # Should still be pending
        logger.info("Task correctly rate limited (remains pending)")

if __name__ == "__main__":
    try:
        test_dispatcher()
        logger.info("All tests passed!")
    except Exception as e:
        logger.error(f"Test failed: {e}")
        sys.exit(1)
