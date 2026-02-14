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


def get_existing_urls(session) -> set:
    """Get all existing LinkedIn URLs from the database."""
    tasks = session.query(Task).filter(Task.type == TaskType.SEND_INVITE).all()
    existing_urls = set()
    for task in tasks:
        try:
            payload = json.loads(task.payload)
            existing_urls.add(payload.get("url"))
        except (json.JSONDecodeError, AttributeError):
            continue
    return existing_urls


def populate_db_from_csv():
    """
    Reads people_all.csv and populates the database with 'send_invite' tasks.
    Filters: rank > 1000, /in/ URLs only, sorts by rank, limits to 10,000.
    """
    csv_path = os.path.join(os.path.dirname(__file__), "people_all.csv")

    if not os.path.exists(csv_path):
        logger.error(f"CSV file not found at: {csv_path}")
        return

    # Initialize DB (ensure tables exist)
    init_db()

    session = SessionLocal()

    try:
        # Get existing URLs from database
        existing_urls = get_existing_urls(session)
        logger.info(f"Found {len(existing_urls)} existing URLs in database")

        # Read and filter CSV
        filtered_rows = []
        with open(csv_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                linkedin_url = row.get("linkedin_url", "").strip()
                rank_str = row.get("rank", "").strip()

                # Filter 1: Skip empty linkedin_url
                if not linkedin_url:
                    continue

                # Filter 2: Skip if /in/ not in URL
                if "/in/" not in linkedin_url:
                    continue

                # Filter 3: Skip empty/invalid rank
                if not rank_str:
                    continue

                try:
                    rank = int(rank_str)
                except ValueError:
                    continue

                # Filter 4: Skip rank <= 1000 (we want rank > 1000)
                if rank <= 1000:
                    continue

                filtered_rows.append({"url": linkedin_url, "rank": rank})

        logger.info(
            f"Filtered {len(filtered_rows)} rows with rank > 1000 and /in/ URLs"
        )

        # Deduplicate by linkedin_url first (keep first occurrence = lowest rank)
        # This must happen before taking 10,000 to get 10,000 unique URLs
        seen_urls = {}
        for row in filtered_rows:
            url = row["url"]
            if url not in seen_urls:
                seen_urls[url] = row

        deduplicated_rows = list(seen_urls.values())
        csv_duplicates = len(filtered_rows) - len(deduplicated_rows)
        logger.info(
            f"After deduplication: {len(deduplicated_rows)} rows ({csv_duplicates} duplicates removed)"
        )

        # Sort by rank ascending
        deduplicated_rows.sort(key=lambda x: x["rank"])

        # Take first 10,000
        deduplicated_rows = deduplicated_rows[:10000]
        logger.info(
            f"After deduplication: {len(deduplicated_rows)} rows ({csv_duplicates} duplicates removed)"
        )

        # Skip URLs already in database
        new_rows = []
        db_duplicates = 0
        for row in deduplicated_rows:
            if row["url"] in existing_urls:
                db_duplicates += 1
            else:
                new_rows.append(row)

        logger.info(
            f"After DB deduplication: {len(new_rows)} new tasks ({db_duplicates} already in DB)"
        )

        # Batch insert: 1000 tasks per commit
        batch_size = 1000
        added_count = 0

        for i in range(0, len(new_rows), batch_size):
            batch = new_rows[i : i + batch_size]
            tasks_to_add = []

            for row in batch:
                payload_dict = {"url": row["url"]}
                new_task = Task(
                    type=TaskType.SEND_INVITE,
                    payload=json.dumps(payload_dict),
                    status=TaskStatus.PENDING,
                )
                tasks_to_add.append(new_task)

            session.add_all(tasks_to_add)
            session.commit()
            added_count += len(tasks_to_add)
            logger.info(
                f"Inserted batch {i // batch_size + 1}: {len(tasks_to_add)} tasks"
            )

        logger.info(
            f"Added {added_count} new tasks, skipped {db_duplicates} duplicates (DB), skipped {csv_duplicates} duplicates (CSV)"
        )

    except Exception as e:
        session.rollback()
        logger.error(f"An error occurred: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    populate_db_from_csv()
