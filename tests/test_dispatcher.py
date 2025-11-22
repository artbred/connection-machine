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

    # Test 2: Rate Limiting and Non-Blocking
    logger.info("Test 2: Rate Limiting and Non-Blocking")
    
    # Insert 50 completed tasks for create_post (limit is 50)
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
        
    # Create a blocked task (create_post) and a non-blocked task (send_invite)
    with SessionLocal() as db:
        blocked_task = Task(
            type=TaskType.CREATE_POST,
            payload=json.dumps({"content": "Blocked"}),
            status=TaskStatus.PENDING
        )
        db.add(blocked_task)
        
        allowed_task = Task(
            type=TaskType.SEND_INVITE,
            payload=json.dumps({"url": "http://example.com"}),
            status=TaskStatus.PENDING
        )
        db.add(allowed_task)
        db.commit()
        blocked_id = blocked_task.id
        allowed_id = allowed_task.id
        
    # Poll should skip blocked task and pick up allowed task
    dispatcher.poll()
    
    with SessionLocal() as db:
        blocked = db.query(Task).filter(Task.id == blocked_id).first()
        allowed = db.query(Task).filter(Task.id == allowed_id).first()
        
        assert blocked.status == TaskStatus.PENDING
        assert allowed.status == TaskStatus.COMPLETED
        logger.info("Rate limiting correctly skipped blocked task and executed allowed task")

    # Test 3: Zombie Cleanup
    logger.info("Test 3: Zombie Cleanup")
    with SessionLocal() as db:
        zombie = Task(
            type=TaskType.CREATE_POST,
            payload=json.dumps({"content": "Zombie"}),
            status=TaskStatus.PROCESSING
        )
        db.add(zombie)
        db.commit()
        zombie_id = zombie.id
        
    dispatcher.cleanup_zombie_tasks()
    
    with SessionLocal() as db:
        zombie = db.query(Task).filter(Task.id == zombie_id).first()
        assert zombie.status == TaskStatus.PENDING
        logger.info("Zombie task correctly reset to PENDING")

    # Test 4: Scheduled Tasks
    logger.info("Test 4: Scheduled Tasks")
    future_time = datetime.utcnow() + timedelta(hours=1)
    with SessionLocal() as db:
        future_task = Task(
            type=TaskType.SEND_INVITE,
            payload=json.dumps({"url": "http://example.com/future"}),
            status=TaskStatus.PENDING,
            scheduled_for=future_time
        )
        db.add(future_task)
        db.commit()
        future_id = future_task.id
        
    dispatcher.poll()
    
    with SessionLocal() as db:
        future = db.query(Task).filter(Task.id == future_id).first()
        assert future.status == TaskStatus.PENDING
        logger.info("Future task correctly ignored")

if __name__ == "__main__":
    try:
        test_dispatcher()
        logger.info("All tests passed!")
    except Exception as e:
        logger.error(f"Test failed: {e}")
        sys.exit(1)
