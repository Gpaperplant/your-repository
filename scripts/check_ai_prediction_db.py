
"""Read-only smoke test for the live EnerCal AI Prediction page queries."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from database import get_connection


ROOT = Path(__file__).resolve().parents[1]
PAGE_PATH = ROOT / "enercal" / "enercal_ai_prediction.py"

REQUIRED_COLUMNS = {
    "enercal_approved_comparison_view": {
        "document_id",
        "technology_role",
        "annual_energy_consumption_kwh_year",
        "calculated_energy_saving_percent",
    },
    "source_documents": {
        "document_id",
        "case_id",
        "filename",
    },
    "modernisation_cases": {
        "case_id",
        "case_ref",
        "elevator_id",
        "status",
    },
    "enercal_specifications": {
        "document_id",
        "field_name",
        "field_value",
    },
}


class _PageStopped(Exception):
    pass


def _import_prediction_page():
    # Stop the Streamlit entry point before it can perform page actions.
    stub = ModuleType("streamlit")
    stub.session_state = {"role": None}

    stub.title = lambda *args, **kwargs: None
    stub.caption = lambda *args, **kwargs: None
    stub.error = lambda *args, **kwargs: None

    def stop():
        raise _PageStopped()

    stub.stop = stop

    with patch.dict(sys.modules, {"streamlit": stub}):
        spec = importlib.util.spec_from_file_location(
            "_ai_prediction_db_smoke_page",
            PAGE_PATH,
        )

        if spec is None or spec.loader is None:
            raise RuntimeError(
                f"Cannot load page: {PAGE_PATH}"
            )

        page = importlib.util.module_from_spec(spec)

        try:
            spec.loader.exec_module(page)
        except _PageStopped:
            pass

    return page


def _query(query, params=()):
    # Enforce a read-only transaction against the project database.
    with get_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(
                    "SET LOCAL statement_timeout = '15s'"
                )

                cur.execute(query, params)

                columns = [
                    item.name for item in cur.description
                ]

                rows = [
                    dict(zip(columns, values))
                    for values in cur.fetchall()
                ]

                return columns, rows


def _check_schema():
    # Inspect schema columns without changing project records.
    errors = []

    for relation, required in REQUIRED_COLUMNS.items():
        try:
            columns, _ = _query(
                f"SELECT * FROM {relation} LIMIT 0"
            )

            missing = sorted(
                required - set(columns)
            )

            if missing:
                errors.append(
                    f"{relation}: missing {', '.join(missing)}"
                )
            else:
                print(
                    f"PASS: {relation} has all required columns"
                )

        except Exception as exc:
            errors.append(
                f"{relation}: {exc}"
            )

    return errors


def _check_page_queries(page):
    # Execute the original page SQL to catch integration mismatches.
    errors = []

    page._read_rows = (
        lambda query, params=(): _query(query, params)[1]
    )

    try:
        reports = page._approved_proposals()

        print(
            "PASS: approved proposal SQL returned "
            f"{len(reports)} row(s)"
        )

    except Exception as exc:
        return [
            f"Approved-proposal query failed: {exc}"
        ]

    try:
        page._specifications(-1)

        print(
            "PASS: engineering-specification SQL executed"
        )

    except Exception as exc:
        errors.append(
            f"Engineering-specification query failed: {exc}"
        )
        return errors

    valid = 0

    for report in reports:
        try:
            specifications = page._specifications(
                report["document_id"]
            )

            page._features(
                report,
                specifications,
                False,
            )

            valid += 1

        except (ValueError, KeyError, TypeError) as exc:
            errors.append(
                f"Document {report.get('document_id')}: "
                f"invalid EnerCal inputs: {exc}"
            )

        except Exception as exc:
            errors.append(
                f"Document {report.get('document_id')}: "
                f"specification query failed: {exc}"
            )

    print(
        f"PASS: {valid}/{len(reports)} proposals "
        "have valid required AI inputs"
    )

    if not reports:
        print(
            "INFO: No eligible unfinished proposals; "
            "the UI should show an empty state"
        )

    print(
        "INFO: No model training or AI prediction was performed"
    )

    return errors


def main():
    print(
        "Checking live EnerCal AI Prediction "
        "database compatibility (read-only)"
    )

    try:
        page = _import_prediction_page()

        errors = _check_schema()

        if not errors:
            errors.extend(
                _check_page_queries(page)
            )

    except Exception as exc:
        errors = [
            f"Smoke test could not start: {exc}"
        ]

    if errors:
        for error in errors:
            print(f"FAIL: {error}")

        print(
            f"FAILED: {len(errors)} issue(s) detected"
        )

        return 1

    print(
        "PASS: Live prediction-page SQL "
        "and data checks completed"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
