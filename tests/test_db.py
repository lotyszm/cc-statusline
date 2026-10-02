import os
import sqlite3
import unittest

from tracker import db
from tests.helpers import IsolatedTestCase

V1_ASSIGNMENTS = """
CREATE TABLE assignments (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, task TEXT,
    effective_from REAL NOT NULL, created_at REAL NOT NULL, origin TEXT NOT NULL
);
INSERT INTO assignments (session_id, task, effective_from, created_at, origin)
VALUES ('s1', 'A-1', 10, 10, 'cli');
PRAGMA user_version = 1;
"""


class MigrationTest(IsolatedTestCase):
    def test_a_version_1_database_keeps_its_data_and_gains_assignment_branches(self):
        path = os.path.join(self.tmp, "v1.db")
        raw = sqlite3.connect(path)
        raw.executescript(V1_ASSIGNMENTS)
        raw.close()
        conn = db.connect(path)
        db.add_assignment(conn, "s1", "B-2", 20, "cli", branch="feat/x")
        self.assertEqual(db.assignments(conn, ["s1"])["s1"], [(10, "A-1", None), (20, "B-2", "feat/x")])
        conn.close()

    def test_a_database_marked_current_but_missing_the_column_is_repaired(self):
        # What a hook racing a half-applied upgrade left behind on 2026-10-01.
        path = os.path.join(self.tmp, "half.db")
        raw = sqlite3.connect(path)
        raw.executescript(V1_ASSIGNMENTS.replace("PRAGMA user_version = 1;", "PRAGMA user_version = 2;"))
        raw.close()
        conn = db.connect(path)
        self.assertEqual(db.assignments(conn, ["s1"])["s1"], [(10, "A-1", None)])
        conn.close()

    def test_connecting_twice_to_a_migrated_database_is_harmless(self):
        path = os.path.join(self.tmp, "v1.db")
        raw = sqlite3.connect(path)
        raw.executescript(V1_ASSIGNMENTS)
        raw.close()
        db.connect(path).close()
        conn = db.connect(path)
        self.assertEqual(db.assignments(conn, ["s1"])["s1"], [(10, "A-1", None)])
        conn.close()


if __name__ == "__main__":
    unittest.main()
