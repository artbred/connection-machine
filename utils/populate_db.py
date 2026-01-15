import csv
import json
import os
import sys
import logging

# Add the project root directory to sys.path so we can import from src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.db import SessionLocal, Task, TaskType, TaskStatus, init_db

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def url_exists_in_db(session, url: str) -> bool:
    """Check if a task with this LinkedIn URL already exists."""
    existing = session.query(Task).filter(
        Task.type == TaskType.SEND_INVITE,
        Task.payload.contains(f'"url": "{url}"')
    ).first()
    return existing is not None


def populate_db_from_csv(csv_path: str):
    """
    Reads a CSV file and populates the database with 'send_invite' tasks
    using the LinkedIn URLs found in the 'LinkedIn URL' column.
    """
    if not os.path.exists(csv_path):
        logger.error(f"CSV file not found at: {csv_path}")
        return

    # Initialize DB (ensure tables exist)
    init_db()

    session = SessionLocal()

    try:
        with open(csv_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)

            added_count = 0
            skipped_count = 0
            for row in reader:
                linkedin_url = row.get("LinkedIn URL")

                if not linkedin_url:
                    continue

                # Check if URL already exists in the database
                if url_exists_in_db(session, linkedin_url):
                    logger.info(f"Skipping duplicate URL: {linkedin_url}")
                    skipped_count += 1
                    continue

                # Create the payload
                payload_dict = {"url": linkedin_url}
                payload_json = json.dumps(payload_dict)

                # Create the task
                new_task = Task(
                    type=TaskType.SEND_INVITE,
                    payload=payload_json,
                    status=TaskStatus.PENDING,
                )

                session.add(new_task)
                added_count += 1

            session.commit()
            logger.info(f"Successfully added {added_count} tasks to the database. Skipped {skipped_count} duplicates.")

    except Exception as e:
        session.rollback()
        logger.error(f"An error occurred: {e}")
    finally:
        session.close()


if __name__ == "__main__":
    # Path to the profiles.csv file in the same directory as this script
    # csv_file_path = os.path.join(os.path.dirname(__file__), "profiles.csv")
    csv_file_path = "/Users/artbred/Documents/projects/_linkedin/cb/data/connections_with_linkedin_linkedin_only.csv"
    populate_db_from_csv(csv_file_path)
