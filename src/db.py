import os
import logging
import enum
from datetime import datetime

from sqlalchemy.orm import (
    Mapped,
    Session,
    declarative_base,
    mapped_column,
    sessionmaker,
)
from sqlalchemy import (
    create_engine,
    Integer,
    Text,
    DateTime,
    func,
    inspect,
    Enum as SqlEnum,
)
from sqlalchemy.schema import CreateColumn

from typing import Generator
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Database Setup
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set")

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker[Session](autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class TaskType(str, enum.Enum):
    SEND_INVITE = "send_invite"
    CREATE_POST = "create_post"
    COMMENT_FEED_POST = "comment_feed_post"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Task(Base):
    __tablename__ = "linkedin_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    type: Mapped[TaskType] = mapped_column(
        SqlEnum(
            TaskType,
            name="linkedin_task_type",
            values_callable=lambda e: [x.value for x in e],
        ),
        nullable=False,
    )
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        SqlEnum(
            TaskStatus,
            name="linkedin_task_status",
            values_callable=lambda e: [x.value for x in e],
        ),
        default=TaskStatus.PENDING,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=True
    )
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    not_before: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    preflight_retries: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

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
    """Create tables and add deferred-preflight fields without rewriting tasks."""
    try:
        if engine.dialect.name == "sqlite":
            db_path = engine.url.database
            if db_path and db_path != ":memory:":
                directory = os.path.dirname(db_path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
        with engine.begin() as connection:
            Base.metadata.create_all(bind=connection)
            existing_columns = {
                column["name"]
                for column in inspect(connection).get_columns(Task.__tablename__)
            }
            table_name = connection.dialect.identifier_preparer.format_table(
                Task.__table__
            )
            for name in ("not_before", "preflight_retries"):
                if name not in existing_columns:
                    column_ddl = CreateColumn(Task.__table__.c[name]).compile(
                        dialect=connection.dialect
                    )
                    connection.exec_driver_sql(
                        f"ALTER TABLE {table_name} ADD {column_ddl}"
                    )
        logger.info("Database initialized and task schema upgraded.")
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
