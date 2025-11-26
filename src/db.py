import os
import logging
import enum

from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    Text,
    DateTime,
    func,
    Enum as SqlEnum,
)

from typing import Generator
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)

# Database Setup
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker[Session](autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class TaskType(str, enum.Enum):
    SEND_INVITE = "send_invite"
    CREATE_POST = "create_post"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    type = Column(SqlEnum(TaskType), nullable=False)
    payload = Column(Text, nullable=False)
    status = Column(SqlEnum(TaskStatus), default=TaskStatus.PENDING)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    executed_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)

    def __repr__(self):
        return f"<Task(id={self.id}, type='{self.type}', status='{self.status}')>"


def get_db() -> Generator[Session, None, None]:
    """Generator for dependency injection or context management."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """
    Initializes the database by creating the connections table if it doesn't exist.
    """
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database initialized and tables checked/created.")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        raise


if __name__ == "__main__":
    # Basic test when running the file directly
    logging.basicConfig(level=logging.INFO)
    try:
        init_db()
        print("Database initialized.")

    except Exception as e:
        print(f"An error occurred: {e}")
