import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import db  # noqa: E402


LEGACY_COLUMNS = "id, type, payload, status, created_at, updated_at, executed_at, error"
LEGACY_SCHEMA = """
CREATE TABLE linkedin_tasks (
    id INTEGER PRIMARY KEY,
    type VARCHAR(17) NOT NULL,
    payload TEXT NOT NULL,
    status VARCHAR(10),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    executed_at DATETIME,
    error TEXT
)
"""


class DatabaseMigrationTests(unittest.TestCase):
    def make_engine(self, url):
        engine = create_engine(url)
        self.addCleanup(engine.dispose)
        return engine

    def initialize(self, engine):
        with patch.object(db, "engine", engine):
            db.init_db()

    def assert_retry_fields_work(self, engine):
        columns = {
            column["name"]: column
            for column in inspect(engine).get_columns("linkedin_tasks")
        }
        self.assertTrue(columns["not_before"]["nullable"])
        self.assertFalse(columns["preflight_retries"]["nullable"])
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO linkedin_tasks (id, type, payload, status) "
                    "VALUES (900, 'send_invite', '{}', 'pending')"
                )
            )
        with Session(engine) as session:
            task = session.get(db.Task, 900)
            self.assertIsNotNone(task)
            self.assertIsNone(task.not_before)
            self.assertEqual(task.preflight_retries, 0)
            self.assertEqual(task.status, db.TaskStatus.PENDING)
            deferred_until = datetime(2026, 10, 6, 13, 30, 15, 123456)
            task.not_before = deferred_until
            task.preflight_retries = 1
            session.commit()
        self.initialize(engine)
        with Session(engine) as session:
            task = session.get(db.Task, 900)
            self.assertEqual(task.not_before, deferred_until)
            self.assertEqual(task.preflight_retries, 1)
            created = db.Task(type=db.TaskType.SEND_INVITE, payload="{}")
            session.add(created)
            session.commit()
            self.assertEqual(created.preflight_retries, 0)
            self.assertIsNone(created.not_before)
        with self.assertRaises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO linkedin_tasks (type, payload, preflight_retries) "
                    "VALUES ('send_invite', '{}', NULL)"
                )
            )

    def test_legacy_schema_upgrade_twice_preserves_every_existing_value(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = self.make_engine(f"sqlite:///{directory}/legacy.db")
            with engine.begin() as connection:
                connection.execute(text(LEGACY_SCHEMA))
                connection.execute(
                    text("CREATE INDEX ix_linkedin_tasks_id ON linkedin_tasks (id)")
                )
                for index, status in enumerate(
                    ("pending", "processing", "completed", "failed")
                ):
                    timestamp = datetime(2026, 10, 5, 10, 20, 30, 123456) + timedelta(
                        hours=index
                    )
                    connection.execute(
                        text(
                            f"INSERT INTO linkedin_tasks ({LEGACY_COLUMNS}) "
                            "VALUES (:id, :type, :payload, :status, :created, :updated, :executed, :error)"
                        ),
                        {
                            "id": 41 + index,
                            "type": "send_invite",
                            "payload": '{ "url": "https://www.linkedin.com/in/ada/", "name": "Ада" }',
                            "status": status,
                            "created": timestamp,
                            "updated": timestamp + timedelta(minutes=1),
                            "executed": timestamp + timedelta(minutes=2)
                            if index >= 2
                            else None,
                            "error": "audience_unavailable"
                            if status == "failed"
                            else None,
                        },
                    )
                original = connection.execute(
                    text(f"SELECT {LEGACY_COLUMNS} FROM linkedin_tasks ORDER BY id")
                ).all()
            original_indexes = inspect(engine).get_indexes("linkedin_tasks")

            for _ in range(2):
                self.initialize(engine)
                with engine.connect() as connection:
                    preserved = connection.execute(
                        text(f"SELECT {LEGACY_COLUMNS} FROM linkedin_tasks ORDER BY id")
                    ).all()
                    self.assertEqual(preserved, original)
                    self.assertEqual(
                        connection.execute(
                            text(
                                "SELECT not_before, preflight_retries FROM linkedin_tasks ORDER BY id"
                            )
                        ).all(),
                        [(None, 0)] * 4,
                    )
                self.assertEqual(
                    inspect(engine).get_indexes("linkedin_tasks"), original_indexes
                )
                with Session(engine) as session:
                    self.assertEqual(
                        [
                            task.status
                            for task in session.query(db.Task).order_by(db.Task.id)
                        ],
                        list(db.TaskStatus),
                    )
            self.assert_retry_fields_work(engine)

    def test_partial_additive_upgrade_keeps_existing_deferral(self):
        engine = self.make_engine("sqlite:///:memory:")
        deferred_until = datetime(2026, 10, 6, 18, 0, 0, 123456)
        with engine.begin() as connection:
            connection.execute(text(LEGACY_SCHEMA))
            connection.execute(
                text("ALTER TABLE linkedin_tasks ADD not_before DATETIME")
            )
            connection.execute(
                text(
                    "INSERT INTO linkedin_tasks (id, type, payload, status, not_before) "
                    "VALUES (41, 'send_invite', '{}', 'pending', :not_before)"
                ),
                {"not_before": deferred_until},
            )
        self.initialize(engine)
        self.initialize(engine)
        with Session(engine) as session:
            task = session.get(db.Task, 41)
            self.assertEqual(task.not_before, deferred_until)
            self.assertEqual(task.preflight_retries, 0)

    def test_fresh_in_memory_database_initializes_twice(self):
        for url in ("sqlite:///:memory:", "sqlite://"):
            with self.subTest(url=url):
                engine = self.make_engine(url)
                self.initialize(engine)
                self.initialize(engine)
                self.assert_retry_fields_work(engine)

    def test_fresh_file_database_creates_missing_parent_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "data" / "tasks.db"
            engine = self.make_engine(f"sqlite:///{path}")
            self.initialize(engine)
            self.initialize(engine)
            self.assertTrue(path.is_file())
            self.assert_retry_fields_work(engine)


if __name__ == "__main__":
    unittest.main()
