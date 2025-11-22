import os
import logging
from typing import Generator
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    Text,
    Boolean,
    DateTime,
    func,
    desc,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)

# Database Setup
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set")

# SQLAlchemy requires the driver prefix to be correct.
# psycopg2 is the default for postgresql:// but explicit is fine.
# If the URL starts with postgres:// (common in some providers), SQLAlchemy prefers postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Connection(Base):
    __tablename__ = "connections"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(Text, nullable=False)
    is_sent = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())
    sent_at = Column(DateTime)

    def __repr__(self):
        return f"<Connection(id={self.id}, url='{self.url}', is_sent={self.is_sent})>"


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


def get_pending_connections(limit: int = 10) -> list[Connection]:
    """
    Retrieves the N latest records from the connections table
    where is_sent is FALSE, ordered by created_at DESC.

    Args:
        limit (int): The number of records to retrieve. Default is 10.

    Returns:
        list[Connection]: A list of Connection objects. Defaults to 10.
    """
    with SessionLocal() as db:
        return db.query(Connection).filter(Connection.is_sent.is_(False)).order_by(desc(Connection.created_at)).limit(limit).all()


if __name__ == "__main__":
    # Basic test when running the file directly
    logging.basicConfig(level=logging.INFO)
    try:
        init_db()

        # Add a test record if empty (optional, just for verification if needed)
        # with SessionLocal() as db:
        #     if db.query(Connection).count() == 0:
        #         db.add(Connection(url="http://example.com/test"))
        #         db.commit()

        pending = get_pending_connections(5)
        print(f"Found {len(pending)} pending connections:")
        for record in pending:
            print(f"ID: {record.id}, URL: {record.url}, Created: {record.created_at}")

    except Exception as e:
        print(f"An error occurred: {e}")
