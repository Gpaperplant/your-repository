"""Static fail-closed safety checks for step 35's reversible archive operation."""
from pathlib import Path
import re

SQL = (Path(__file__).resolve().parents[1] / "scripts" / "035_archive_legacy_mod.sql").read_text()
LIVE = "\n".join(line.split("--", 1)[0] for line in SQL.splitlines()).lower()


def test_transaction_rehearsal_by_default():
    assert "begin;" in LIVE
    assert "rollback;" in LIVE
    assert "\\if :{?apply}" in LIVE
    assert "\\if :apply" in LIVE
    assert "commit;" in LIVE
    assert "on_error_stop" in LIVE


def test_preserves_five_table_case_and_meter_observations():
    for name in ("test-mod-rev-001", "project_documents", "document_revisions", "engineering_values", "document_reviews", "energy_observations", "verified_measurement_pairs"):
        assert name in LIVE
    for guard in ("v_mod_docs <> 1", "v_revisions <> 2", "v_values <> 126", "v_meter_pairs <> 0", "v_observations < 47"):
        assert guard in LIVE


def test_only_moves_six_legacy_mod_tables():
    moves = re.findall(r"alter\s+table\s+public\.(\w+)\s+set\s+schema\s+legacy_archive", LIVE)
    assert set(moves) == {"source_documents", "document_pages", "extraction_revisions", "extraction_candidates", "enercal_specifications", "enercal_review_events"}
    assert len(moves) == 6


def test_no_destructive_operations():
    for forbidden in ("drop table", "drop view", "cascade", "truncate", "delete from", "disable trigger"):
        assert forbidden not in LIVE
