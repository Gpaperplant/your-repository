
"""Test the AI prediction page without reading or changing project records."""

import importlib.util
import sys
import unittest
from contextlib import nullcontext
from math import isfinite
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from services.ai_dataset import FEATURE_COLUMNS, SPEC_FIELDS
from services.ai_training import predict_saving, train_and_evaluate


PAGE_PATH = (
    Path(__file__).resolve().parents[1]
    / "enercal"
    / "enercal_ai_prediction.py"
)


class PageStopped(Exception):
    pass


class FakeStreamlit(ModuleType):
    # Simulate only the Streamlit controls required by the prediction page.
    def __init__(
        self,
        role="Admin",
        result=None,
        submitted=False,
        specs_verified=False,
    ):
        super().__init__("streamlit")

        self.session_state = {"role": role}

        if result is not None:
            self.session_state["enercal_ai_training_result"] = result

        self.submitted = submitted
        self.specs_verified = specs_verified
        self.messages = []
        self.tables = []
        self.metrics = []

    def _message(self, kind, *args, **kwargs):
        self.messages.append(
            (kind, " ".join(map(str, args)))
        )

    def title(self, *args, **kwargs):
        self._message("title", *args, **kwargs)

    def caption(self, *args, **kwargs):
        self._message("caption", *args, **kwargs)

    def info(self, *args, **kwargs):
        self._message("info", *args, **kwargs)

    def warning(self, *args, **kwargs):
        self._message("warning", *args, **kwargs)

    def error(self, *args, **kwargs):
        self._message("error", *args, **kwargs)

    def write(self, *args, **kwargs):
        self._message("write", *args, **kwargs)

    def subheader(self, *args, **kwargs):
        self._message("subheader", *args, **kwargs)

    def columns(self, count):
        return [self] * count

    def metric(self, label, value, **kwargs):
        self.metrics.append((label, value))

    def dataframe(self, data, **kwargs):
        self.tables.append(data)

    def selectbox(self, label, options, **kwargs):
        return options[0]

    def expander(self, *args, **kwargs):
        return nullcontext()

    def form(self, *args, **kwargs):
        return nullcontext()

    def checkbox(self, *args, **kwargs):
        return (
            self.specs_verified
            and not kwargs.get("disabled", False)
        )

    def form_submit_button(self, *args, **kwargs):
        return self.submitted

    def stop(self):
        raise PageStopped()


def import_page(ui):
    # Page import executes main, so use the no-model state during unit setup.
    with patch.dict(sys.modules, {"streamlit": ui}):
        spec = importlib.util.spec_from_file_location(
            "_isolated_enercal_ai_prediction",
            PAGE_PATH,
        )

        page = importlib.util.module_from_spec(spec)

        try:
            spec.loader.exec_module(page)
        except PageStopped:
            pass

        return page


def synthetic_dataset(count=12):
    # These are fictional values for testing, never verified KPI evidence.
    dataset = []

    for index in range(count):
        prediction = 30 + index
        before = 2000 + index * 50

        row = dict.fromkeys(FEATURE_COLUMNS)

        row.update({
            "case_id": index + 1,
            "document_id": index + 1,
            "case_ref": f"TEST-MOD-{index + 1:03d}",
            "elevator_id": index + 1,
            "predicted_saving_percent": prediction,
            "estimated_before_kwh_year": before,
            "estimated_after_kwh_year": before * (
                1 - prediction / 100
            ),
            "measured_saving_percent": (
                prediction - 3 + index % 3
            ),
            "target_correction_pp": -3 + index % 3,
            "nominal_load_kg": 630 + index * 20,
        })

        dataset.append(row)

    return dataset


def sample_report():
    return {
        "document_id": 20,
        "filename": "synthetic-proposal.pdf",
        "case_id": 15,
        "case_ref": "TEST-MOD-015",
        "elevator_id": 200,
        "case_status": "pending",
        "estimated_before_kwh_year": 10000,
        "estimated_after_kwh_year": 5500,
        "predicted_saving_percent": 45,
    }


class TestAIPredictionPage(unittest.TestCase):

    def setUp(self):
        self.ui = FakeStreamlit()
        self.page = import_page(self.ui)

    def test_no_model_does_not_query_database(self):
        with patch.object(
            self.page,
            "_approved_proposals",
        ) as query:
            self.page.main()

        query.assert_not_called()

        self.assertTrue(
            any(
                kind == "warning"
                for kind, _ in self.ui.messages
            )
        )

    def test_non_admin_cannot_query_database(self):
        ui = FakeStreamlit(role="Requester")

        with patch.dict(sys.modules, {"streamlit": ui}):
            with patch("database.get_connection") as connection:
                import_page(ui)

        connection.assert_not_called()

    def test_proposal_sql_uses_approved_comparison_and_unfinished_cases(self):
        with patch.object(
            self.page,
            "_read_rows",
            return_value=[],
        ) as reader:
            self.page._approved_proposals()

        query = reader.call_args.args[0]

        self.assertIn(
            "FROM enercal_approved_comparison_view v",
            query,
        )
        self.assertIn(
            "m.status <> 'completed'",
            query,
        )
        self.assertIn(
            "AS estimated_before_kwh_year",
            query,
        )
        self.assertIn(
            "AS estimated_after_kwh_year",
            query,
        )
        self.assertIn(
            "AS predicted_saving_percent",
            query,
        )
        self.assertIn(
            "v.technology_role = 'existing'",
            query,
        )
        self.assertIn(
            "v.technology_role = 'proposed'",
            query,
        )

    def test_specification_sql_uses_expected_fields(self):
        with patch.object(
            self.page,
            "_read_rows",
            return_value=[],
        ) as reader:
            self.page._specifications(20)

        query, params = reader.call_args.args

        self.assertIn(
            "FROM enercal_specifications",
            query,
        )
        self.assertIn(
            "field_name = ANY(%s)",
            query,
        )
        self.assertEqual(params[0], 20)
        self.assertEqual(
            set(params[1]),
            set(SPEC_FIELDS),
        )

    def test_features_match_training_columns_and_mask_unverified_specs(self):
        specifications = [
            {
                "field_name": "existing_nominal_load_kg",
                "field_value": "1000",
            }
        ]

        masked = self.page._features(
            sample_report(),
            specifications,
            False,
        )

        verified = self.page._features(
            sample_report(),
            specifications,
            True,
        )

        self.assertEqual(
            set(masked),
            set(FEATURE_COLUMNS),
        )

        self.assertEqual(
            masked["predicted_saving_percent"],
            45.0,
        )

        self.assertIsNone(
            masked["nominal_load_kg"]
        )

        self.assertEqual(
            verified["nominal_load_kg"],
            1000.0,
        )

        self.assertNotIn(
            "measured_saving_percent",
            verified,
        )

        self.assertNotIn(
            "target_correction_pp",
            verified,
        )

    def test_invalid_or_inconsistent_enercal_values_are_rejected(self):
        invalid_reports = (
            {"estimated_before_kwh_year": 0},
            {"estimated_after_kwh_year": -1},
            {"estimated_after_kwh_year": 5400},
            {"predicted_saving_percent": None},
        )

        for changes in invalid_reports:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.page._features(
                        {
                            **sample_report(),
                            **changes,
                        },
                        [],
                        False,
                    )

    def test_existing_training_model_predicts_from_page_features(self):
        result = train_and_evaluate(
            synthetic_dataset()
        )

        features = self.page._features(
            sample_report(),
            [],
            False,
        )

        prediction = predict_saving(
            result,
            features,
        )

        self.assertEqual(
            set(prediction),
            {
                "enercal_saving_percent",
                "ai_adjusted_saving_percent",
                "correction_pp",
            },
        )

        self.assertEqual(
            prediction["enercal_saving_percent"],
            45,
        )

        self.assertTrue(
            isfinite(
                prediction["ai_adjusted_saving_percent"]
            )
        )

        self.assertLessEqual(
            prediction["ai_adjusted_saving_percent"],
            100,
        )

    def test_full_page_renders_real_model_prediction(self):
        result = train_and_evaluate(
            synthetic_dataset()
        )

        ui = FakeStreamlit(
            result=result,
            submitted=True,
            specs_verified=True,
        )

        page = import_page(FakeStreamlit())

        with (
            patch.object(
                page,
                "_approved_proposals",
                return_value=[sample_report()],
            ),
            patch.object(
                page,
                "_specifications",
                return_value=[
                    {
                        "field_name": "existing_nominal_load_kg",
                        "field_value": "1000",
                    }
                ],
            ),
            patch.object(page, "st", ui),
        ):
            page.main()

        self.assertEqual(
            [label for label, _ in ui.metrics],
            [
                "EnerCal saving",
                "AI-adjusted saving",
                "AI correction",
            ],
        )

        self.assertEqual(
            len(ui.tables[-1]),
            3,
        )

        self.assertFalse(
            any(
                kind == "error"
                for kind, _ in ui.messages
            )
        )

    def test_no_submission_does_not_call_prediction(self):
        result = train_and_evaluate(
            synthetic_dataset()
        )

        ui = FakeStreamlit(
            result=result,
            submitted=False,
        )

        page = import_page(FakeStreamlit())

        with (
            patch.object(
                page,
                "_approved_proposals",
                return_value=[sample_report()],
            ),
            patch.object(
                page,
                "_specifications",
                return_value=[],
            ),
            patch.object(
                page,
                "predict_saving",
            ) as predictor,
            patch.object(
                page,
                "st",
                ui,
            ),
        ):
            page.main()

        predictor.assert_not_called()

        self.assertEqual(
            ui.metrics,
            [],
        )


if __name__ == "__main__":
    unittest.main()
