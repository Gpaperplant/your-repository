"""Safety checks for the local ISO evidence reset script."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DatabaseCleanupSafetyTests(unittest.TestCase):
    def test_reset_is_scoped_and_guarded(self):
        sql = (ROOT / "database/reset_iso_dev_data.sql").read_text().lower()
        for table in ("iso_assessments", "iso_documents", "iso_extracted_values"):
            self.assertIn("public." + table, sql)
        self.assertIn("current_database() <> 'request_app'", sql)
        self.assertIn("inet_server_addr()", sql)
        self.assertIn("restart identity", sql)
        self.assertNotIn(" cascade", sql.split("truncate table", 1)[1])
        self.assertNotIn("drop table", sql)
        self.assertNotIn("delete from", sql)

    def test_schema_audit_is_read_only(self):
        sql = (ROOT / "database/inspect_database.sql").read_text().lower()
        for forbidden in ("truncate", "drop table", "delete from", "alter table"):
            self.assertNotIn(forbidden, sql)


if __name__ == "__main__":
    unittest.main()
