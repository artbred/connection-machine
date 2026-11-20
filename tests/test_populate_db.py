import csv
import json
import os
import tempfile
import unittest
from itertools import chain
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
from utils import populate_db as importer


class PopulateDBTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        self.sessions = sessionmaker(engine)
        self.table = importer.Task.__table__
        self.table.create(engine)
        self.enterContext(patch.object(importer, "SessionLocal", self.sessions))
        self.enterContext(patch.object(importer, "init_db"))
        self.csv_path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "fixture.csv"

    def populate(self, rows):
        with self.csv_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(("linkedin_url", "rank"))
            writer.writerows(rows)
        importer.populate_db_from_csv(self.csv_path)

    def urls(self, after=0):
        table = self.table.c
        with self.sessions() as session:
            return [json.loads(p)["url"] for p in session.scalars(
                select(table.payload).where(table.id > after).order_by(table.id)
            )]

    def test_first_eligible_exact_url_and_stable_top_10000_before_db_filter(self):
        with self.sessions.begin() as session:
            session.execute(self.table.insert(), [
                {"type": "create_post", "payload": '{"url":"/in/FIRST"}'},
                {"type": "send_invite", "payload": "bad json"},
                {"type": "send_invite", "payload": "[]"},
                {"type": "send_invite", "payload": '{"url":"/in/blocked"}'},
                {"type": "send_invite", "payload": '{"url":"/in/blocked"}'},
            ])
        rows = [
            ("/in/too-far", 10**30), ("/in/too-far", 1001),
            ("/in/blocked", 1002), (" /in/first ", 1002), ("/in/first", 1001),
            ("/in/FIRST", 1001), ("x/in/☃", 1000), ("x/in/☃", "invalid"),
            ("x/in/☃", "+001001"), ("/company/no", 1001),
            ("/in/before-filler", 10**24),
        ]
        self.populate(chain(rows, ((f"/in/{i}", 10**25) for i in range(10000))))
        self.assertEqual(self.urls(after=5), [
            "/in/FIRST", "x/in/☃", "/in/first", "/in/before-filler",
            *(f"/in/{i}" for i in range(9995)),
        ])

    def test_failed_batch_rolls_back_even_after_a_large_payload_flush(self):
        with self.sessions.begin() as session:
            session.connection().exec_driver_sql("""
                CREATE TRIGGER reject_fixture BEFORE INSERT ON linkedin_tasks
                WHEN NEW.payload LIKE '%/in/reject%' BEGIN SELECT RAISE(ABORT, 'fixture'); END
            """)
        first_batch = [f"/in/{i}" for i in range(1000)]
        long_urls = (f"/in/{'a' * 100000}{i}" for i in range(12))
        with self.assertRaises(IntegrityError):
            self.populate((url, 1001) for url in chain(first_batch, long_urls, ["/in/reject"]))
        self.assertEqual(self.urls(), first_batch)
