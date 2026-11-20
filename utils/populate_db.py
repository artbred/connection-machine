import csv
import json
import logging
import os
import sqlite3
import sys
from contextlib import closing
from itertools import islice

# Add the project root directory to sys.path so we can import from src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.db import SessionLocal, Task, TaskType, TaskStatus, init_db

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _select_candidates(spool, csv_path):
    # WITHOUT ROWID stores each URL only once. The first qualifying row wins.
    spool.execute(
        "CREATE TABLE candidates (url TEXT PRIMARY KEY, rank TEXT, seq INTEGER) WITHOUT ROWID"
    )
    add = spool.cursor()
    filtered = 0
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = row.get("linkedin_url", "").strip()
            rank_str = row.get("rank", "").strip()
            if "/in/" not in url:
                continue
            try:
                rank = int(rank_str)
            except ValueError:
                continue
            if rank > 1000:
                add.execute(
                    "INSERT OR IGNORE INTO candidates VALUES (?, ?, ?)",
                    (url, str(rank), filtered),
                )
                filtered += 1
    csv_duplicates = filtered - spool.total_changes
    # Positive decimal strings ordered by length then digits retain Python's
    # integer ordering even for ranks too large for SQLite's signed integers.
    spool.execute("CREATE TABLE selected (url TEXT UNIQUE)")
    selected = spool.execute(
        "INSERT INTO selected SELECT url FROM candidates "
        "ORDER BY length(rank), rank, seq LIMIT 10000"
    ).rowcount
    return selected, csv_duplicates


def _existing_urls(session):
    payloads = session.query(Task.payload).filter(Task.type == TaskType.SEND_INVITE)
    for (payload,) in payloads.yield_per(1):
        try:
            url = json.loads(payload).get("url")
        except (json.JSONDecodeError, AttributeError):
            continue
        if isinstance(url, str):
            # Escaped lone surrogates are legal in stored JSON but cannot match UTF-8 CSV.
            yield (url.encode("utf-8", errors="surrogatepass"),)
        elif isinstance(url, (dict, list)):
            raise TypeError("Unhashable URL in stored invite payload")


def populate_db_from_csv(
    csv_path=os.path.join(os.path.dirname(__file__), "people_all.csv"),
):
    """Queue the first 10,000 unique eligible CSV URLs by rank, less existing invites."""

    if not os.path.exists(csv_path):
        logger.error(f"CSV file not found at: {csv_path}")
        return

    init_db()

    session = SessionLocal()

    try:
        # An empty SQLite filename creates a private disk-backed spool deleted on close.
        with closing(sqlite3.connect("")) as spool:
            spool.execute("PRAGMA temp_store=FILE")
            spool.execute("PRAGMA journal_mode=OFF")  # Only scratch data, never the queue.
            selected, csv_duplicates = _select_candidates(spool, csv_path)
            db_duplicates = spool.executemany(
                "DELETE FROM selected WHERE url = CAST(? AS TEXT)", _existing_urls(session)
            ).rowcount
            new_count = selected - db_duplicates
            rows = spool.execute("SELECT url FROM selected ORDER BY rowid")
            insert = Task.__table__.insert().values(
                type=TaskType.SEND_INVITE, status=TaskStatus.PENDING
            )
            for offset in range(0, new_count, 1000):
                batch = []
                size = 0
                for (url,) in islice(rows, 1000):
                    payload = json.dumps({"url": url})
                    batch.append({"payload": payload})
                    size += len(payload)
                    # Long URLs must not turn a 1,000-row transaction into a huge buffer.
                    if size >= 1024 * 1024:
                        session.execute(insert, batch)
                        batch.clear()
                        size = 0
                if batch:
                    session.execute(insert, batch)
                session.commit()
                logger.info(
                    "Inserted batch %d: %d tasks",
                    offset // 1000 + 1, min(1000, new_count - offset),
                )
            logger.info(
                "Added %d new tasks, skipped %d duplicates (DB), skipped %d duplicates (CSV)",
                new_count, db_duplicates, csv_duplicates,
            )

    except Exception as e:
        session.rollback()
        logger.error(f"An error occurred: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    populate_db_from_csv()
