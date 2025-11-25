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

            count = 0
            for row in reader:
                linkedin_url = row.get("LinkedIn URL")

                if not linkedin_url:
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

                if count > 200:
                    session.add(new_task)
                    
                count += 1

            session.commit()
            logger.info(f"Successfully added {count} tasks to the database.")

    except Exception as e:
        session.rollback()
        logger.error(f"An error occurred: {e}")
    finally:
        session.close()


if __name__ == "__main__":
    # Path to the profiles.csv file in the same directory as this script
    csv_file_path = os.path.join(os.path.dirname(__file__), "profiles.csv")
    populate_db_from_csv(csv_file_path)
