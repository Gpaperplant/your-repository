"""MOD upload regression tests for new-only five-table evidence storage."""

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPARISON_FIELDS = (
    "annual_energy_consumption", "annual_regenerative_energy",
    "annual_electricity_expenditure", "lifetime_electricity_expenditure",
    "operational_co2e", "embodied_co2e",
)


class FakeCursor:
    def __init__(self):
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, statement, params=()):
        self.queries.append((statement, params))
        raise AssertionError("The MOD repository must use only unified insert helpers")


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()
        self.committed = False
        self.rolled_back = False
        self.transaction_count = 0

    def __enter__(self):
        self.transaction_count += 1
        return self

    def __exit__(self, error, *_):
        self.committed = error is None
        self.rolled_back = error is not None
        return False

    def cursor(self):
        return self.cursor_instance


@pytest.fixture
def extracted():
    comparison = {
        "source_page": 4,
        "currency_symbol": "$",
        "lifetime_years": 25,
        "rows": {
            "existing": {
                "elevator_technology": "Typical Traction Elevator",
                "annual_energy_consumption": Decimal("4213"),
                "annual_regenerative_energy": None,
                "annual_electricity_expenditure": 1138,
                "lifetime_electricity_expenditure": 36450,
                "operational_co2e": 40661,
                "embodied_co2e": None,
            },
            "proposed": {
                "elevator_technology": "ReGenerate 400 DX",
                "annual_energy_consumption": Decimal("1993"),
                "annual_regenerative_energy": 450,
                "annual_electricity_expenditure": 538,
                "lifetime_electricity_expenditure": 17232,
                "operational_co2e": 19230,
                "embodied_co2e": 0,
            },
        },
        "reported_energy_saving_percent": Decimal("53"),
        "calculated_energy_saving_percent": Decimal("52.69"),
    }
    specs = [
        ("existing_nominal_load_kg", "630", "kg", 8),
        ("proposed_nominal_load_kg", "630", "kg", 8),
        ("existing_nominal_speed_m_s", "1", "m/s", 8),
        ("proposed_nominal_speed_m_s", "1.6", "m/s", 8),
        ("travel_distance_m", "15", "m", 8),
        ("served_floors", "6", None, 8),
        ("starts_per_year", "273750", "starts/year", 8),
        ("operating_days_per_year", "365", "days/year", 8),
        ("iso_usage_category", "4", None, 8),
        ("existing_gear", "Geared", None, 8),
        ("proposed_gear", "Gearless", None, 8),
        ("existing_drive_type", "V3F induction", None, 8),
        ("proposed_drive_type", "V3F PMSM", None, 8),
        ("existing_roping_suspension_type", "1:1", None, 8),
        ("proposed_roping_suspension_type", "2:1", None, 8),
        ("existing_regenerative_drive", "No", None, 8),
        ("proposed_regenerative_drive", "Yes", None, 8),
    ]
    return {
        "comparison": comparison,
        "pages": [{"page_number": number, "extracted_text": f"page {number}"}
                  for number in range(1, 13)],
        "specifications": specs,
    }


@pytest.fixture
def setup(monkeypatch, extracted):
    connection = FakeConnection()
    database = ModuleType("database")
    database.get_connection = lambda: connection
    extractor = ModuleType("services.enercal_extractor")
    extractor.COMPARISON_FIELDS = COMPARISON_FIELDS
    extractor.extract_mod_document = Mock(return_value=extracted)
    specs = ModuleType("services.enercal_specs")
    specs.extract_specs = Mock(return_value=extracted["specifications"])
    monkeypatch.setitem(sys.modules, "database", database)
    monkeypatch.setitem(sys.modules, "services.enercal_extractor", extractor)
    monkeypatch.setitem(sys.modules, "services.enercal_specs", specs)

    path = ROOT / "services/enercal_repository.py"
    spec = importlib.util.spec_from_file_location("_mod_unified_save", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.insert_project_document = Mock(return_value={
        "document_id": 301, "case_id": 9, "created": True,
    })
    module.insert_revision = Mock(return_value={
        "revision_id": 401, "document_id": 301,
        "revision_no": 1, "review_status": "pending",
    })
    module.insert_engineering_values = Mock(
        side_effect=lambda _cur, _id, values: {
            "revision_id": _id, "values_saved": len(values),
        }
    )
    return module, connection, extractor, specs


def save(module, extracted=None):
    return module.save_complete_enercal_report(
        "MOD1001", "enercal.pdf", b"%PDF-1.7 demo", extracted=extracted,
    )


def test_returns_only_unified_ids_and_actual_record_counts(setup, extracted):
    module, connection, _, _ = setup
    saved = save(module, extracted)
    assert saved["document_id"] == 301
    assert saved["revision_id"] == 401
    assert saved["revision_no"] == 1
    assert saved["review_scope"] == "full_values"
    assert saved["pages_extracted"] == 12
    assert saved["comparison_fields_saved"] == 16
    assert saved["additional_fields_extracted"] == 17
    assert saved["values_saved"] == 33
    assert "unified_document_id" not in saved
    assert "pages_saved" not in saved
    assert connection.committed


def test_one_transaction_and_no_legacy_sql_writes(setup, extracted):
    module, connection, _, _ = setup
    save(module, extracted)
    assert connection.transaction_count == 1
    assert connection.cursor_instance.queries == []
    cursor = connection.cursor_instance
    assert module.insert_project_document.call_args.args[0] is cursor
    assert module.insert_revision.call_args.args[0] is cursor
    assert module.insert_engineering_values.call_args.args[0] is cursor
    module.insert_project_document.assert_called_once()
    module.insert_revision.assert_called_once_with(cursor, 301)
    module.insert_engineering_values.assert_called_once()


def test_original_pdf_and_case_sent_to_unified_store(setup, extracted):
    module, _, _, _ = setup
    save(module, extracted)
    kwargs = module.insert_project_document.call_args.kwargs
    assert kwargs["document_type"] == "mod_pdf"
    assert kwargs["file_bytes"] == b"%PDF-1.7 demo"
    assert kwargs["case_ref"] == "MOD1001"
    assert kwargs["filename"] == "enercal.pdf"
    assert kwargs["source_classification"] == "unknown"
    # Test-lift cases are classified by insert_project_document as test_fixture.


def test_mod_features_can_be_generated_without_legacy_storage(setup, extracted):
    from services.mod_value_mapper import mod_feature_record
    module, _, _, _ = setup
    save(module, extracted)
    values = module.insert_engineering_values.call_args.args[2]
    features = mod_feature_record(values)
    assert len(features) == 26
    assert features["MOD_PROXY_energy_existing_kwh_year"] == 4213
    assert features["MOD_PROXY_energy_proposed_kwh_year"] == 1993
    assert all(value["source_verified"] is False for value in values)


def test_duplicate_in_unified_store_rolls_back_without_writes(setup, extracted):
    module, connection, _, _ = setup
    module.insert_project_document.return_value["created"] = False
    with pytest.raises(ValueError, match="already saved"):
        save(module, extracted)
    assert connection.rolled_back
    module.insert_revision.assert_not_called()
    module.insert_engineering_values.assert_not_called()


@pytest.mark.parametrize("failed", ["document", "revision", "values"])
def test_each_storage_failure_rolls_back_transaction(setup, extracted, failed):
    module, connection, _, _ = setup
    {"document": module.insert_project_document,
     "revision": module.insert_revision,
     "values": module.insert_engineering_values}[failed].side_effect = RuntimeError("simulated failure")
    with pytest.raises(RuntimeError, match="simulated failure"):
        save(module, extracted)
    assert connection.rolled_back


def test_value_count_mismatch_aborts_transaction(setup, extracted):
    module, connection, _, _ = setup
    module.insert_engineering_values.side_effect = None
    module.insert_engineering_values.return_value = {"values_saved": 1}
    with pytest.raises(RuntimeError, match="Incomplete"):
        save(module, extracted)
    assert connection.rolled_back


def test_invalid_pdf_before_any_write(setup, extracted):
    module, connection, _, _ = setup
    with pytest.raises(ValueError, match="valid MOD PDF"):
        module.save_complete_enercal_report("MOD1001", "x.pdf", b"garbage", extracted)
    assert connection.transaction_count == 0
    module.insert_project_document.assert_not_called()


def test_oversized_pdf_before_any_write(setup, extracted):
    module, connection, _, _ = setup
    with pytest.raises(ValueError, match="15 MB"):
        module.save_complete_enercal_report(
            "MOD1001", "x.pdf", b"%PDF-" + b"0" * module.MAX_PDF_BYTES, extracted,
        )
    assert connection.transaction_count == 0


def test_invalid_page_count_before_any_write(setup, extracted):
    module, _, _, _ = setup
    extracted["pages"].pop()
    with pytest.raises(ValueError, match="12 pages"):
        save(module, extracted)
    module.insert_project_document.assert_not_called()


def test_duplicate_specification_prevents_write(setup, extracted):
    module, _, _, _ = setup
    extracted["specifications"].append(extracted["specifications"][0])
    with pytest.raises(ValueError, match="Repeated MOD"):
        save(module, extracted)
    module.insert_project_document.assert_not_called()


def test_wrong_comparison_page_prevents_write(setup, extracted):
    module, _, _, _ = setup
    extracted["comparison"]["source_page"] = 5
    with pytest.raises(ValueError, match="page 4"):
        save(module, extracted)
    module.insert_project_document.assert_not_called()


def test_missing_comparison_field_prevents_write(setup, extracted):
    module, _, _, _ = setup
    del extracted["comparison"]["rows"]["existing"]["annual_energy_consumption"]
    with pytest.raises(ValueError, match="annual_energy_consumption"):
        save(module, extracted)
    module.insert_project_document.assert_not_called()


def test_uses_supplied_preview_without_reextracting(setup, extracted):
    module, _, extractor, specs = setup
    save(module, extracted)
    extractor.extract_mod_document.assert_not_called()
    specs.extract_specs.assert_not_called()


def test_extracts_pdf_and_specs_when_preview_not_supplied(setup, extracted):
    module, _, extractor, specs = setup
    extractor.extract_mod_document.return_value = {
        key: value for key, value in extracted.items() if key != "specifications"
    }
    save(module)
    extractor.extract_mod_document.assert_called_once_with(b"%PDF-1.7 demo")
    specs.extract_specs.assert_called_once_with(b"%PDF-1.7 demo")


def test_missing_specs_in_preview_extracts_only_specs(setup, extracted):
    module, _, extractor, specs = setup
    extracted.pop("specifications")
    save(module, extracted)
    extractor.extract_mod_document.assert_not_called()
    specs.extract_specs.assert_called_once()


def test_source_values_are_unverified_and_not_marked_corrected(setup, extracted):
    module, _, _, _ = setup
    save(module, extracted)
    values = module.insert_engineering_values.call_args.args[2]
    assert all(value["source_verified"] is False for value in values)
    assert all(value["is_corrected"] is False for value in values)


def test_legacy_storage_names_not_present_in_repository():
    code = (ROOT / "services/enercal_repository.py").read_text()
    forbidden = (
        "source_documents", "extraction_revisions", "document_pages",
        "extraction_candidates", "enercal_specifications", "enercal_review_events",
    )
    assert all(name not in code for name in forbidden)
