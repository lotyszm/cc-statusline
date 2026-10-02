"""Guards for the oldest supported setup: Python 3.9 and SQLite without upserts.

Running the whole suite under python3.9 is the real check; these catch the
usual slips on whatever version runs the tests.
"""

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted([ROOT / "statusline.py", *(ROOT / "tracker").glob("*.py"), *(ROOT / "tests").glob("*.py")])


class PortabilityTest(unittest.TestCase):
    def test_sources_parse_with_the_python_3_9_grammar(self):
        for path in SOURCES:
            with self.subTest(path.name):
                ast.parse(path.read_text(encoding="utf-8"), str(path), feature_version=(3, 9))

    def test_sql_avoids_upserts(self):
        # ON CONFLICT ... DO UPDATE needs SQLite 3.24.
        upsert = re.compile(r"ON\s+CONFLICT\b[^;]*?\bDO\s+UPDATE", re.I | re.S)
        for path in (ROOT / "tracker").glob("*.py"):
            with self.subTest(path.name):
                self.assertIsNone(upsert.search(path.read_text(encoding="utf-8")))

    def test_tomllib_is_imported_with_a_fallback(self):
        for path in (ROOT / "tracker").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            if re.search(r"^\s*import tomllib", text, re.M):
                with self.subTest(path.name):
                    self.assertIn("except ImportError", text)


if __name__ == "__main__":
    unittest.main()
