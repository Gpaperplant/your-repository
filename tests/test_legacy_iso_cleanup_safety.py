"""Static safety checks on the optional Step 34 SQL."""
from pathlib import Path
import unittest

SQL = Path(__file__).resolve().parents[1] / "scripts" / "034_retire_legacy_iso_only.sql"


class CleanupSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = SQL.read_text(encoding="utf-8")
        cls.statements = "\n".join(
            line.split("--", 1)[0] for line in cls.sql.splitlines()
        ).lower()

    def test_transaction_and_fail_closed(self):
        self.assertIn("begin;", self.statements)
        self.assertIn("commit;", self.statements)
        self.assertIn("on_error_stop", self.statements)
        self.assertIn("lock_timeout", self.statements)
        self.assertIn("raise exception", self.statements)

    def test_never_cascades_or_disables_triggers(self):
        self.assertNotIn("cascade", self.statements)
        self.assertNotIn("disable trigger", self.statements)
        self.assertNotIn("truncate", self.statements)
        self.assertNotIn("delete from", self.statements)

    def test_only_drops_three_legacy_iso_tables(self):
        import re
        drops = re.findall(r"drop\s+table\s+if\s+exists\s+([\w.]+)", self.statements)
        self.assertEqual(drops, ["public.iso_extracted_values", "public.iso_documents", "public.iso_assessments"])

    def test_keeps_named_case_and_audits(self):
        self.assertIn("test-mod-rev-001", self.statements)
        self.assertIn("public.document_reviews", self.statements)
        self.assertIn("public.engineering_values", self.statements)


if __name__ == "__main__":
    unittest.main()
