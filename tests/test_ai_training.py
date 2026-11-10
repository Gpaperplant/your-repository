
import unittest
from math import isfinite
from unittest.mock import patch

from services.ai_dataset import FEATURE_COLUMNS, build_ai_dataset, model_inputs
from services.ai_training import predict_saving, train_and_evaluate


def sample_report(document_id=1, case_id=1, elevator_id=1):
    # These are synthetic examples, not actual measured projects.
    report = dict.fromkeys(FEATURE_COLUMNS)
    report.update({
        "document_id": document_id,
        "case_id": case_id,
        "case_ref": f"TEST-MOD-{case_id:03d}",
        "elevator_id": elevator_id,
        "predicted_saving_percent": 55.0,
        "estimated_before_kwh_year": 2000.0,
        "estimated_after_kwh_year": 900.0,
        "nominal_load_kg": 1000.0,
    })
    return report


def sample_pair(document_id=1, case_id=1, elevator_id=1):
    return {
        "document_id": document_id,
        "case_id": case_id,
        "elevator_id": elevator_id,
        "review_status": "approved",
        "before_basis": "metered",
        "after_basis": "metered",
        "before_unit": "kWh",
        "after_unit": "kWh",
        "conditions_verified": True,
        "specifications_verified": True,
        "before_source_id": 101,
        "after_source_id": 102,
        "before_kwh": 240.0,
        "after_kwh": 120.0,
        "before_days": 30,
        "after_days": 30,
    }


def sample_dataset(size=16):
    rows = []

    for index in range(size):
        row = sample_report(index + 1, index + 1, index + 1)
        row["predicted_saving_percent"] = 35.0 + index
        row["estimated_before_kwh_year"] = 2000.0 + index * 100
        row["estimated_after_kwh_year"] = (
            row["estimated_before_kwh_year"]
            * (1 - row["predicted_saving_percent"] / 100)
        )
        row["measured_saving_percent"] = (
            row["predicted_saving_percent"] - 4 + index % 3
        )
        row["target_correction_pp"] = (
            row["measured_saving_percent"]
            - row["predicted_saving_percent"]
        )
        row["nominal_load_kg"] = (
            None if index % 3 == 0 else 630 + index * 20
        )
        rows.append(row)

    return rows


class TestAIDataset(unittest.TestCase):
    def test_verified_pair_builds_correct_target(self):
        with patch(
            "services.ai_dataset.load_approved_enercal",
            return_value={1: sample_report()},
        ):
            accepted, excluded = build_ai_dataset([sample_pair()])

        self.assertEqual(excluded, [])
        self.assertEqual(len(accepted), 1)
        self.assertAlmostEqual(
            accepted[0]["measured_saving_percent"], 50.0
        )
        self.assertAlmostEqual(
            accepted[0]["target_correction_pp"], -5.0
        )

    def test_unverified_pairs_are_rejected(self):
        changes = (
            {"review_status": "pending"},
            {"before_basis": "calculated"},
            {"after_unit": "Wh"},
            {"conditions_verified": False},
            {"elevator_id": 999},
            {"after_source_id": 101},
            {"before_days": 0},
        )

        for change in changes:
            with self.subTest(change=change):
                pair = sample_pair()
                pair.update(change)

                with patch(
                    "services.ai_dataset.load_approved_enercal",
                    return_value={1: sample_report()},
                ):
                    accepted, excluded = build_ai_dataset([pair])

                self.assertEqual(accepted, [])
                self.assertEqual(len(excluded), 1)

    def test_unverified_specs_are_not_used_as_inputs(self):
        pair = sample_pair()
        pair["specifications_verified"] = False

        with patch(
            "services.ai_dataset.load_approved_enercal",
            return_value={1: sample_report()},
        ):
            accepted, excluded = build_ai_dataset([pair])

        self.assertEqual(excluded, [])
        self.assertIsNone(accepted[0]["nominal_load_kg"])

    def test_duplicate_case_pairs_are_rejected(self):
        with patch(
            "services.ai_dataset.load_approved_enercal",
            return_value={1: sample_report()},
        ):
            accepted, excluded = build_ai_dataset([
                sample_pair(), sample_pair()
            ])

        self.assertEqual(accepted, [])
        self.assertEqual(len(excluded), 2)

    def test_model_inputs_do_not_expose_metered_targets(self):
        features, targets, groups = model_inputs(sample_dataset(1))

        self.assertEqual(set(features[0]), set(FEATURE_COLUMNS))
        self.assertNotIn("measured_saving_percent", features[0])
        self.assertEqual(targets, [-4.0])
        self.assertEqual(groups, [1])


class TestAITraining(unittest.TestCase):
    def test_training_keeps_each_elevator_in_one_split(self):
        records = sample_dataset(24)

        for index, row in enumerate(records):
            row["elevator_id"] = index // 2 + 1

        result = train_and_evaluate(records)
        test_cases = {
            row["case_ref"]
            for row in result["test_predictions"]
        }

        for elevator_id in {
            row["elevator_id"] for row in records
        }:
            cases = {
                row["case_ref"]
                for row in records
                if row["elevator_id"] == elevator_id
            }
            self.assertTrue(
                cases <= test_cases
                or cases.isdisjoint(test_cases)
            )

        self.assertEqual(
            result["test_projects"], len(test_cases)
        )
        self.assertEqual(
            result["train_projects"] + result["test_projects"],
            len(records),
        )

        for metrics in result["metrics"].values():
            self.assertTrue(
                all(isfinite(value) for value in metrics.values())
            )

    def test_prediction_accepts_missing_optional_features(self):
        trained = train_and_evaluate(sample_dataset())

        predicted = predict_saving(
            trained,
            {"predicted_saving_percent": 50.0},
        )

        self.assertTrue(
            isfinite(predicted["ai_adjusted_saving_percent"])
        )
        self.assertLessEqual(
            predicted["ai_adjusted_saving_percent"], 100
        )

    def test_too_few_elevators_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "At least 10"):
            train_and_evaluate(sample_dataset(9))

    def test_duplicate_case_is_rejected(self):
        records = sample_dataset()
        records[1]["case_id"] = records[0]["case_id"]

        with self.assertRaisesRegex(ValueError, "Duplicate case_id"):
            train_and_evaluate(records)

    def test_invalid_correction_is_rejected(self):
        records = sample_dataset()
        records[0]["target_correction_pp"] = 80.0

        with self.assertRaisesRegex(ValueError, "does not match"):
            train_and_evaluate(records)


if __name__ == "__main__":
    unittest.main()
