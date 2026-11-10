"""Export auditable, held-out AI evaluation from verified meter readings.

This module does not modify project data or persist an AI model. It trains the
existing prototype evaluator only when genuine eligible evidence is sufficient,
then matches its *held-out* predictions to the same independently verified
meter-reading pairs used in the measured-vs-EnerCal reporting service.

The exports omit personal contact fields, but case references and elevator IDs
remain internal identifiers and should be handled as sensitive project data.
Ineligible and pending cases are never filled with synthetic data.
"""

import argparse
import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sys
import tempfile
import unittest


METHODS = (
    ("enercal", "EnerCal estimate"),
    ("bias_correction", "Average bias correction"),
    ("ridge_ai", "Ridge AI adjustment"),
)

PREDICTION_COLUMNS = (
    "case_ref", "document_id", "elevator_id", "pair_id",
    "before_kwh_per_day", "after_kwh_per_day",
    "measured_saving_percent", "enercal_saving_percent",
    "bias_adjusted_saving_percent", "ridge_ai_saving_percent",
    "enercal_error_pp", "bias_adjusted_error_pp", "ridge_ai_error_pp",
)
METRIC_COLUMNS = ("method", "test_projects", "test_elevators", "mae_pp", "rmse_pp", "bias_pp")


def _number(value, label):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label}: missing or invalid numeric value")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{label}: missing or invalid numeric value") from error
    if not number.is_finite():
        raise ValueError(f"{label}: number must be finite")
    return number


def _identity(row, origin):
    """Use both case reference and elevator identity; reject missing IDs."""
    case_ref = str(row.get("case_ref") or "").strip()
    elevator_id = row.get("elevator_id")
    if not case_ref or isinstance(elevator_id, bool) or elevator_id is None:
        raise ValueError(f"{origin}: missing case/elevator identity")
    return case_ref, str(elevator_id)


def _index_unique(rows, origin):
    lookup = {}
    for row in rows:
        key = _identity(row, origin)
        if key in lookup:
            raise ValueError(f"{origin}: duplicate case/elevator {key}")
        lookup[key] = row
    return lookup


def _check_close(left, right, label, tolerance=Decimal("0.02")):
    if abs(_number(left, label) - _number(right, label)) > tolerance:
        raise ValueError(f"{label}: AI and verified measurement data disagree")


def build_holdout_comparison(dataset, evaluation, verified_pairs):
    """Link held-out predictions to independently verified energy measurements.

    Fail closed on missing pairs, duplicate case/elevator records, different
    document IDs, or inconsistent measured and EnerCal percentages.
    Returns only hold-out rows. No trained-project predictions are exported.
    """
    predictions = evaluation.get("test_predictions")
    if not isinstance(predictions, list) or not predictions:
        raise ValueError("No held-out AI predictions are available")

    evidence_by_key = _index_unique(dataset, "AI dataset")
    verified_by_key = _index_unique(verified_pairs, "Verified measurement pairs")
    prediction_by_key = _index_unique(predictions, "Held-out predictions")

    rows = []
    for key, predicted in sorted(prediction_by_key.items()):
        record = evidence_by_key.get(key)
        measurement = verified_by_key.get(key)
        if record is None or measurement is None:
            raise ValueError(f"{key}: missing matching AI or verified meter evidence")
        if (record.get("document_id") is None
                or record.get("document_id") != measurement.get("document_id")):
            raise ValueError(f"{key}: EnerCal document IDs do not match")
        if measurement.get("pair_id") is None:
            raise ValueError(f"{key}: verified pair identity is missing")

        _check_close(
            predicted.get("measured_saving_percent"),
            measurement.get("measured_saving_percent"),
            f"{key}: observed saving",
        )
        _check_close(
            record.get("measured_saving_percent"),
            measurement.get("measured_saving_percent"),
            f"{key}: training target",
        )
        _check_close(
            predicted.get("enercal_percent"),
            record.get("predicted_saving_percent"),
            f"{key}: EnerCal estimate",
        )
        _check_close(
            predicted.get("enercal_percent"),
            measurement.get("enercal_estimated_saving_percent"),
            f"{key}: approved EnerCal estimate",
        )

        actual = _number(predicted["measured_saving_percent"], "Measured saving")
        enercal = _number(predicted["enercal_percent"], "EnerCal saving")
        bias = _number(predicted["bias_correction_percent"], "Bias adjusted saving")
        ridge = _number(predicted["ridge_ai_percent"], "Ridge AI saving")

        rows.append({
            "case_ref": key[0],
            "document_id": record["document_id"],
            "elevator_id": record["elevator_id"],
            "pair_id": measurement["pair_id"],
            "before_kwh_per_day": measurement["measured_before_kwh_per_day"],
            "after_kwh_per_day": measurement["measured_after_kwh_per_day"],
            "measured_saving_percent": actual,
            "enercal_saving_percent": enercal,
            "bias_adjusted_saving_percent": bias,
            "ridge_ai_saving_percent": ridge,
            # Signed model error: positive = estimate exceeds observed saving.
            "enercal_error_pp": enercal - actual,
            "bias_adjusted_error_pp": bias - actual,
            "ridge_ai_error_pp": ridge - actual,
        })

    if len(rows) != evaluation.get("test_projects"):
        raise ValueError("Held-out row count disagrees with the model evaluation")
    if len({str(r["elevator_id"]) for r in rows}) != evaluation.get("test_elevators"):
        raise ValueError("Held-out elevator count disagrees with the model evaluation")

    return rows


def evaluation_metrics(evaluation):
    """Retain the model's evaluated metrics, without recalculating on training data."""
    metrics = evaluation.get("metrics") or {}
    result = []
    for method_key, method_label in METHODS:
        if method_key not in metrics:
            raise ValueError(f"Missing evaluation method: {method_key}")
        item = metrics[method_key]
        result.append({
            "method": method_label,
            "test_projects": evaluation["test_projects"],
            "test_elevators": evaluation["test_elevators"],
            "mae_pp": _number(item["mae_pp"], "MAE"),
            "rmse_pp": _number(item["rmse_pp"], "RMSE"),
            "bias_pp": _number(item["bias_pp"], "Bias"),
        })
    return result


def write_evaluation_files(folder, comparison, metrics):
    """Write two CSVs after all comparisons and numeric fields validate."""
    if not comparison or len(metrics) != len(METHODS):
        raise ValueError("Refusing to export empty or incomplete AI evaluation")
    path = Path(folder)
    path.mkdir(parents=True, exist_ok=True)
    outputs = [
        (path / "heldout_ai_vs_enercal_vs_meter.csv", PREDICTION_COLUMNS, comparison),
        (path / "heldout_ai_metrics.csv", METRIC_COLUMNS, metrics),
    ]
    for filename, columns, records in outputs:
        with filename.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for record in records:
                writer.writerow(record)
    return [name for name, _, _ in outputs]


def export_live_evaluation(folder, test_size=0.25):
    """Read live verified evidence, evaluate on unseen elevators, then export."""
    # Import trained-model and database dependencies only for the live command.
    from services.ai_measurement_loader import prepare_ai_dataset
    from services.ai_training import MIN_ELEVATORS, train_and_evaluate
    from services.verified_savings_reporting import load_verified_savings

    dataset, excluded = prepare_ai_dataset()
    elevator_count = len({row["elevator_id"] for row in dataset})
    print(f"Eligible AI projects: {len(dataset)}")
    print(f"Independent verified elevators: {elevator_count}")
    print(f"Excluded pairs: {len(excluded)}")

    if elevator_count < MIN_ELEVATORS:
        print(f"Not ready: at least {MIN_ELEVATORS} eligible distinct elevators are required.")
        print("No model trained and no evaluation CSV files created.")
        return []

    evaluation = train_and_evaluate(dataset, test_size=test_size, random_state=42)
    verified_pairs = load_verified_savings()
    comparison = build_holdout_comparison(dataset, evaluation, verified_pairs)
    metrics = evaluation_metrics(evaluation)
    filenames = write_evaluation_files(folder, comparison, metrics)
    for filename in filenames:
        print(f"Exported: {filename}")
    print("Only held-out predictions from unseen elevators are included.")
    print("No model or training data is persisted in the database.")
    return filenames


class _SelfTests(unittest.TestCase):
    def sample(self):
        dataset = [{
            "case_ref": "CASE-1", "elevator_id": 11, "document_id": 53,
            "measured_saving_percent": 50,
            "predicted_saving_percent": 40,
        }]
        evaluation = {
            "test_projects": 1, "test_elevators": 1,
            "test_predictions": [{
                "case_ref": "CASE-1", "elevator_id": 11,
                "measured_saving_percent": 50.001,
                "enercal_percent": 40.001,
                "bias_correction_percent": 43,
                "ridge_ai_percent": 48,
            }],
            "metrics": {
                key: {"mae_pp": 2.0, "rmse_pp": 2.0, "bias_pp": 1.0}
                for key, _ in METHODS
            },
        }
        verified = [{
            "case_ref": "CASE-1", "elevator_id": 11, "document_id": 53,
            "pair_id": 12,
            "measured_saving_percent": Decimal("50"),
            "enercal_estimated_saving_percent": Decimal("40"),
            "measured_before_kwh_per_day": Decimal("10"),
            "measured_after_kwh_per_day": Decimal("5"),
        }]
        return dataset, evaluation, verified

    def test_heldout_comparison(self):
        dataset, evaluation, verified = self.sample()
        rows = build_holdout_comparison(dataset, evaluation, verified)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ridge_ai_error_pp"], Decimal("-2.001"))
        self.assertEqual(rows[0]["pair_id"], 12)

    def test_reject_mismatched_document(self):
        dataset, evaluation, verified = self.sample()
        verified[0]["document_id"] = 99
        with self.assertRaisesRegex(ValueError, "document IDs"):
            build_holdout_comparison(dataset, evaluation, verified)

    def test_reject_unrelated_meter_readings(self):
        dataset, evaluation, verified = self.sample()
        verified[0]["measured_saving_percent"] = Decimal("10")
        with self.assertRaisesRegex(ValueError, "disagree"):
            build_holdout_comparison(dataset, evaluation, verified)

    def test_reject_duplicate_measurement_identity(self):
        dataset, evaluation, verified = self.sample()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            build_holdout_comparison(dataset, evaluation, verified + verified)

    def test_reject_missing_enercal_approval(self):
        dataset, evaluation, verified = self.sample()
        verified[0]["enercal_estimated_saving_percent"] = None
        with self.assertRaisesRegex(ValueError, "invalid numeric"):
            build_holdout_comparison(dataset, evaluation, verified)

    def test_reject_heldout_count_mismatch(self):
        dataset, evaluation, verified = self.sample()
        evaluation["test_elevators"] = 2
        with self.assertRaisesRegex(ValueError, "count disagrees"):
            build_holdout_comparison(dataset, evaluation, verified)

    def test_only_approved_heldout_metrics(self):
        _, evaluation, _ = self.sample()
        rows = evaluation_metrics(evaluation)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2]["method"], "Ridge AI adjustment")

    def test_csv_contains_no_personal_contact_fields(self):
        dataset, evaluation, verified = self.sample()
        prediction_rows = build_holdout_comparison(dataset, evaluation, verified)
        with tempfile.TemporaryDirectory() as temp:
            outputs = write_evaluation_files(temp, prediction_rows, evaluation_metrics(evaluation))
            self.assertEqual(len(outputs), 2)
            csv_text = outputs[0].read_text(encoding="utf-8-sig")
            self.assertIn("CASE-1", csv_text)
            self.assertNotIn("contact_email", csv_text)
            self.assertNotIn("private@example.test", csv_text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="Run self-contained tests (no database)")
    parser.add_argument("--output-dir", default="reports/ai_evaluation", help="CSV output folder")
    parser.add_argument("--test-size", type=float, default=0.25, help="Elevator holdout fraction")
    args = parser.parse_args(argv)

    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(_SelfTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1

    try:
        export_live_evaluation(args.output_dir, test_size=args.test_size)
        return 0
    except (ValueError, KeyError, OSError, RuntimeError) as error:
        print(f"ERROR: AI evaluation export was not created: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        # Database driver errors should also be visible without a traceback dump.
        print(f"ERROR: Could not evaluate verified data: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
