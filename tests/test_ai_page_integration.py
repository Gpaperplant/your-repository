
"""Verify the AI page contract without accessing project data."""

import importlib.util
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from services import ai_dataset, ai_measurement_loader, ai_training


PAGE_PATH = (
    Path(__file__).resolve().parents[1]
    / "enercal"
    / "enercal_ai_training.py"
)


class PageStopped(Exception):
    pass


class FakeStreamlit(ModuleType):
    # Simulate the Streamlit controls used by the existing AI page.
    def __init__(
        self,
        role="Admin",
        buttons=None,
        test_percent=25,
        session=None,
    ):
        super().__init__("streamlit")
        self.session_state = dict(session or {})
        self.session_state.setdefault("role", role)
        self.buttons = dict(buttons or {})
        self.test_percent = test_percent
        self.messages = []
        self.tables = []
        self.charts = []

    def _message(self, kind, *args, **kwargs):
        self.messages.append(
            (kind, " ".join(str(arg) for arg in args))
        )

    def title(self, *args, **kwargs):
        self._message("title", *args, **kwargs)

    def subheader(self, *args, **kwargs):
        self._message("subheader", *args, **kwargs)

    def caption(self, *args, **kwargs):
        self._message("caption", *args, **kwargs)

    def info(self, *args, **kwargs):
        self._message("info", *args, **kwargs)

    def warning(self, *args, **kwargs):
        self._message("warning", *args, **kwargs)

    def success(self, *args, **kwargs):
        self._message("success", *args, **kwargs)

    def error(self, *args, **kwargs):
        self._message("error", *args, **kwargs)

    def write(self, *args, **kwargs):
        self._message("write", *args, **kwargs)

    def metric(self, label, value, **kwargs):
        self.messages.append(("metric", (label, value)))

    def dataframe(self, data, **kwargs):
        self.tables.append(data)

    def bar_chart(self, data, **kwargs):
        self.charts.append((data, kwargs))

    def columns(self, count):
        return [self] * count

    def button(self, label, **kwargs):
        return self.buttons.get(label, False)

    def slider(self, label, **kwargs):
        return self.test_percent

    def expander(self, label, **kwargs):
        return nullcontext()

    def spinner(self, label, **kwargs):
        return nullcontext()

    def divider(self):
        pass

    def stop(self):
        raise PageStopped()


def synthetic_evidence(count=12):
    # Synthetic values are used only in automated tests.
    reports = {}
    pairs = []

    for index in range(count):
        number = index + 1
        predicted = 35 + number * 0.6
        estimated_before = 2000 + number * 45
        estimated_after = estimated_before * (
            1 - predicted / 100
        )

        report = dict.fromkeys(ai_dataset.FEATURE_COLUMNS)

        report.update({
            "case_id": number,
            "document_id": number,
            "case_ref": f"TEST-MOD-{number:03d}",
            "elevator_id": number,
            "predicted_saving_percent": predicted,
            "estimated_before_kwh_year": estimated_before,
            "estimated_after_kwh_year": estimated_after,
            "nominal_load_kg": 630 + number * 20,
        })

        reports[number] = report

        before_kwh = 300 + number * 5
        measured_saving = predicted - 3 + number % 3

        pairs.append({
            "pair_id": number,
            "case_id": number,
            "document_id": number,
            "elevator_id": number,
            "review_status": "approved",
            "before_basis": "metered",
            "after_basis": "metered",
            "before_unit": "kWh",
            "after_unit": "kWh",
            "conditions_verified": True,
            "specifications_verified": True,
            "before_source_id": number * 2,
            "after_source_id": number * 2 + 1,
            "before_kwh": before_kwh,
            "after_kwh": before_kwh * (
                1 - measured_saving / 100
            ),
            "before_days": 30,
            "after_days": 30,
        })

    return pairs, reports


def load_synthetic_dataset(count=12):
    # Exercise the real loader and dataset builder without a database.
    pairs, reports = synthetic_evidence(count)

    with (
        patch.object(
            ai_measurement_loader,
            "load_verified_measurement_pairs",
            return_value=pairs,
        ),
        patch.object(
            ai_dataset,
            "load_approved_enercal",
            return_value=reports,
        ),
    ):
        return ai_measurement_loader.prepare_ai_dataset()


def render_page(fake_streamlit, dataset, excluded, training=None):
    # Execute the page using its existing service interfaces.
    with (
        patch.dict(
            sys.modules,
            {"streamlit": fake_streamlit},
        ),
        patch.object(
            ai_measurement_loader,
            "prepare_ai_dataset",
            return_value=(dataset, excluded),
        ) as loader,
        patch.object(
            ai_training,
            "train_and_evaluate",
            side_effect=training or ai_training.train_and_evaluate,
        ) as trainer,
    ):
        spec = importlib.util.spec_from_file_location(
            "_test_enercal_ai_training_page",
            PAGE_PATH,
        )

        module = importlib.util.module_from_spec(spec)

        try:
            spec.loader.exec_module(module)
        except PageStopped:
            pass

        return loader, trainer


class TestAIPageIntegration(unittest.TestCase):

    def test_loader_and_trainer_output_contract(self):
        dataset, excluded = load_synthetic_dataset()

        self.assertEqual(excluded, [])
        self.assertEqual(len(dataset), 12)
        self.assertEqual(
            len({row["elevator_id"] for row in dataset}),
            12,
        )

        result = ai_training.train_and_evaluate(dataset)

        self.assertEqual(
            set(result["metrics"]),
            {
                "enercal",
                "bias_correction",
                "ridge_ai",
            },
        )

        self.assertTrue(result["test_predictions"])

        self.assertEqual(
            set(result["test_predictions"][0]),
            {
                "case_ref",
                "elevator_id",
                "measured_saving_percent",
                "enercal_percent",
                "bias_correction_percent",
                "ridge_ai_percent",
            },
        )

    def test_no_verified_data_blocks_training(self):
        ui = FakeStreamlit(
            buttons={"Train and evaluate": True}
        )

        loader, trainer = render_page(ui, [], [])

        loader.assert_called_once_with()
        trainer.assert_not_called()

        self.assertTrue(
            any(
                kind == "warning"
                for kind, _ in ui.messages
            )
        )

        self.assertFalse(ui.charts)

    def test_ready_data_trains_and_displays_evaluation(self):
        dataset, excluded = load_synthetic_dataset()

        ui = FakeStreamlit(
            buttons={"Train and evaluate": True}
        )

        loader, trainer = render_page(
            ui,
            dataset,
            excluded,
        )

        loader.assert_called_once_with()
        trainer.assert_called_once()

        self.assertEqual(
            trainer.call_args.kwargs["test_size"],
            0.25,
        )

        result = ui.session_state[
            "enercal_ai_training_result"
        ]

        self.assertEqual(
            result["train_elevators"]
            + result["test_elevators"],
            12,
        )

        self.assertTrue(ui.charts)
        self.assertGreaterEqual(len(ui.tables), 3)

        self.assertFalse(
            any(
                kind == "error"
                for kind, _ in ui.messages
            )
        )

    def test_unverified_role_cannot_access_loader(self):
        ui = FakeStreamlit(role="Requester")

        loader, trainer = render_page(ui, [], [])

        loader.assert_not_called()
        trainer.assert_not_called()

        self.assertTrue(
            any(
                kind == "error"
                for kind, _ in ui.messages
            )
        )

    def test_reload_removes_stale_training_result(self):
        ui = FakeStreamlit(
            buttons={"Reload verified dataset": True},
            session={
                "enercal_ai_dataset": [
                    {"elevator_id": 1}
                ],
                "enercal_ai_excluded": [],
                "enercal_ai_training_result": {
                    "stale": True
                },
            },
        )

        loader, trainer = render_page(ui, [], [])

        loader.assert_called_once_with()
        trainer.assert_not_called()

        self.assertNotIn(
            "enercal_ai_training_result",
            ui.session_state,
        )

        self.assertEqual(
            ui.session_state["enercal_ai_dataset"],
            [],
        )

    def test_training_error_is_displayed_without_stale_results(self):
        dataset, excluded = load_synthetic_dataset()

        ui = FakeStreamlit(
            buttons={"Train and evaluate": True}
        )

        _, trainer = render_page(
            ui,
            dataset,
            excluded,
            training=ValueError(
                "Synthetic training failure"
            ),
        )

        trainer.assert_called_once()

        self.assertNotIn(
            "enercal_ai_training_result",
            ui.session_state,
        )

        self.assertTrue(
            any(
                "Synthetic training failure" in text
                for _, text in ui.messages
            )
        )


if __name__ == "__main__":
    unittest.main()
