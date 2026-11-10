"""Read-only, in-memory tests for the AI page's held-out CSV export controls.

These tests execute the real enercal_ai_training.py and real
services.ai_evaluation_export.py using synthetic evidence. They never access
PostgreSQL, run a real model, or modify approvals/project records.
"""

import csv
import importlib.util
import io
import sys
import unittest
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import ai_evaluation_export


PAGE_PATH = ROOT / "enercal" / "enercal_ai_training.py"


class PageStopped(Exception):
    pass


class FakeStreamlit(ModuleType):
    """Supply only the Streamlit calls exercised by the real AI training page."""

    def __init__(self, role="Admin", session=None, buttons=None, test_percent=25):
        super().__init__("streamlit")
        self.session_state = dict(session or {})
        self.session_state.setdefault("role", role)
        self.buttons = buttons or {}
        self.test_percent = test_percent
        self.messages = []
        self.downloads = []
        self.tables = []
        self.charts = []

    def _message(self, kind, *args, **kwargs):
        self.messages.append((kind, " ".join(map(str, args))))

    def title(self, *args, **kwargs):
        self._message("title", *args)

    def subheader(self, *args, **kwargs):
        self._message("subheader", *args)

    def caption(self, *args, **kwargs):
        self._message("caption", *args)

    def info(self, *args, **kwargs):
        self._message("info", *args)

    def warning(self, *args, **kwargs):
        self._message("warning", *args)

    def success(self, *args, **kwargs):
        self._message("success", *args)

    def error(self, *args, **kwargs):
        self._message("error", *args)

    def write(self, *args, **kwargs):
        self._message("write", *args)

    def metric(self, label, value, **kwargs):
        self._message("metric", label, value)

    def dataframe(self, data, **kwargs):
        self.tables.append(data)

    def bar_chart(self, data, **kwargs):
        self.charts.append(data)

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

    def download_button(self, label, data, file_name, mime, **kwargs):
        self.downloads.append({
            "label": label, "data": data, "file_name": file_name, "mime": mime
        })

    def stop(self):
        raise PageStopped()


def synthetic_evidence(count=12):
    """Return synthetic, internally consistent records; never write them to DB."""
    dataset = []
    verified = []
    for i in range(1, count + 1):
        record = {
            "case_ref": f"TEST-MOD-{i:03d}",
            "elevator_id": i,
            "document_id": i + 100,
            "predicted_saving_percent": Decimal("37"),
            "measured_saving_percent": Decimal("40"),
            "target_correction_pp": Decimal("3"),
        }
        dataset.append(record)
        verified.append({
            "case_ref": record["case_ref"],
            "elevator_id": i,
            "document_id": record["document_id"],
            "pair_id": i + 300,
            "measured_saving_percent": Decimal("40"),
            "enercal_estimated_saving_percent": Decimal("37"),
            "measured_before_kwh_per_day": Decimal("10"),
            "measured_after_kwh_per_day": Decimal("6"),
        })
    return dataset, verified


def sample_evaluation(dataset):
    heldout = dataset[-3:]
    return {
        "train_projects": len(dataset) - len(heldout),
        "train_elevators": len(dataset) - len(heldout),
        "test_projects": len(heldout),
        "test_elevators": len(heldout),
        "feature_names": ["estimated_before_kwh_year"],
        "test_predictions": [
            {
                "case_ref": row["case_ref"],
                "elevator_id": row["elevator_id"],
                "measured_saving_percent": Decimal("40"),
                "enercal_percent": Decimal("37"),
                "bias_correction_percent": Decimal("38"),
                "ridge_ai_percent": Decimal("39"),
            }
            for row in heldout
        ],
        "metrics": {
            key: {"mae_pp": 1.0, "rmse_pp": 1.2, "bias_pp": -0.5}
            for key in ("enercal", "bias_correction", "ridge_ai")
        },
    }


def render(dataset=(), verified=(), result=None, session=None, buttons=None,
           percent=25, role="Admin", training_failure=None):
    ui = FakeStreamlit(role=role, session=session, buttons=buttons, test_percent=percent)
    fake_loader = ModuleType("services.ai_measurement_loader")
    fake_loader.prepare_ai_dataset = Mock(return_value=(list(dataset), []))
    fake_training = ModuleType("services.ai_training")
    fake_training.MIN_ELEVATORS = 10
    fake_training.train_and_evaluate = Mock(
        side_effect=training_failure if training_failure is not None else None,
        return_value=result,
    )
    fake_measured = ModuleType("services.verified_savings_reporting")
    fake_measured.load_verified_savings = Mock(return_value=list(verified))

    with patch.dict(sys.modules, {
        "streamlit": ui,
        "services.ai_measurement_loader": fake_loader,
        "services.ai_training": fake_training,
        "services.verified_savings_reporting": fake_measured,
        "services.ai_evaluation_export": ai_evaluation_export,
    }):
        spec = importlib.util.spec_from_file_location("_test_ai_export_page", PAGE_PATH)
        page = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(page)
        except PageStopped:
            pass

    return ui, fake_loader.prepare_ai_dataset, fake_training.train_and_evaluate, fake_measured.load_verified_savings


class AIExportControlsTests(unittest.TestCase):
    def setUp(self):
        self.dataset, self.verified = synthetic_evidence()
        self.evaluation = sample_evaluation(self.dataset)

    def trained_session(self):
        # Simulate a completed evaluation on the current slider setting.
        return {
            "enercal_ai_dataset": self.dataset,
            "enercal_ai_excluded": [],
            "enercal_ai_training_result": self.evaluation,
            "enercal_ai_test_percent": 25,
        }

    def test_no_verified_measurements_does_not_train_or_export(self):
        ui, _, train, measured = render(
            buttons={"Train and evaluate": True, "Prepare held-out evaluation CSVs": True}
        )
        train.assert_not_called()
        measured.assert_not_called()
        self.assertFalse(ui.downloads)
        self.assertTrue(any(kind == "warning" for kind, _ in ui.messages))

    def test_non_admin_is_blocked_before_reading_evidence(self):
        ui, load, train, measured = render(role="Requester")
        load.assert_not_called()
        train.assert_not_called()
        measured.assert_not_called()
        self.assertFalse(ui.downloads)

    def test_ready_dataset_not_exported_before_training(self):
        ui, _, train, measured = render(dataset=self.dataset, verified=self.verified)
        train.assert_not_called()
        measured.assert_not_called()
        self.assertFalse(ui.downloads)

    def test_training_then_export_uses_same_holdout_without_retraining(self):
        ui, _, train, _ = render(
            dataset=self.dataset, verified=self.verified, result=self.evaluation,
            buttons={"Train and evaluate": True},
        )
        train.assert_called_once()
        self.assertFalse(ui.downloads)

        later, _, train_again, measured = render(
            dataset=self.dataset, verified=self.verified,
            session=ui.session_state,
            buttons={"Prepare held-out evaluation CSVs": True},
        )
        train_again.assert_not_called()
        measured.assert_called_once()
        self.assertEqual(len(later.downloads), 2)
        self.assertEqual(
            {item["file_name"] for item in later.downloads},
            {"heldout_ai_vs_enercal_vs_meter.csv", "heldout_ai_metrics.csv"},
        )
        csv_file = next(item for item in later.downloads if "vs_meter" in item["file_name"])
        rows = list(csv.DictReader(io.StringIO(csv_file["data"].decode("utf-8-sig"))))
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["case_ref"] for row in rows},
                         {row["case_ref"] for row in self.dataset[-3:]})
        self.assertNotIn(self.dataset[0]["case_ref"], csv_file["data"].decode("utf-8-sig"))
        self.assertTrue(all(row["enercal_error_pp"] == "-3" for row in rows))

        metrics_file = next(item for item in later.downloads if "metrics" in item["file_name"])
        metric_rows = list(csv.DictReader(io.StringIO(metrics_file["data"].decode("utf-8-sig"))))
        self.assertEqual(len(metric_rows), 3)

    def test_invalid_verified_evidence_disables_export_but_keeps_evaluation(self):
        wrong = [dict(item) for item in self.verified]
        wrong[-1]["document_id"] = -999
        ui, _, train, _ = render(
            dataset=self.dataset, verified=wrong, session=self.trained_session(),
            buttons={"Prepare held-out evaluation CSVs": True},
        )
        train.assert_not_called()
        self.assertFalse(ui.downloads)
        self.assertIn("enercal_ai_training_result", ui.session_state)
        self.assertTrue(any("document IDs do not match" in msg for _, msg in ui.messages))

    def test_reloading_dataset_clears_saved_evaluation_and_exports(self):
        session = self.trained_session()
        session["enercal_ai_evaluation_exports"] = [{"filename": "stale.csv", "contents": b"stale"}]
        ui, load, train, _ = render(
            dataset=[], verified=[], session=session,
            buttons={"Reload verified dataset": True},
        )
        load.assert_called_once()
        train.assert_not_called()
        self.assertNotIn("enercal_ai_training_result", ui.session_state)
        self.assertNotIn("enercal_ai_evaluation_exports", ui.session_state)
        self.assertFalse(ui.downloads)

    def test_changing_test_percentage_clears_saved_evaluation_and_exports(self):
        session = self.trained_session()
        session["enercal_ai_evaluation_exports"] = [{"filename": "stale.csv", "contents": b"stale"}]
        ui, _, train, _ = render(
            dataset=self.dataset, verified=self.verified,
            session=session, percent=35,
        )
        train.assert_not_called()
        self.assertNotIn("enercal_ai_training_result", ui.session_state)
        self.assertNotIn("enercal_ai_evaluation_exports", ui.session_state)
        self.assertFalse(ui.downloads)

    def test_retraining_invalidates_previous_exports(self):
        session = self.trained_session()
        session["enercal_ai_evaluation_exports"] = [{"filename": "stale.csv", "contents": b"stale"}]
        ui, _, train, _ = render(
            dataset=self.dataset, verified=self.verified, result=self.evaluation,
            session=session, buttons={"Train and evaluate": True},
        )
        train.assert_called_once()
        self.assertNotIn("enercal_ai_evaluation_exports", ui.session_state)
        self.assertFalse(ui.downloads)

    def test_failed_export_clears_old_downloads(self):
        session = self.trained_session()
        session["enercal_ai_evaluation_exports"] = [{"filename": "stale.csv", "contents": b"stale"}]
        ui, _, _, _ = render(
            dataset=self.dataset, verified=[], session=session,
            buttons={"Prepare held-out evaluation CSVs": True},
        )
        self.assertFalse(ui.downloads)
        self.assertNotIn("enercal_ai_evaluation_exports", ui.session_state)
        self.assertTrue(any("Cannot prepare AI evaluation exports" in msg
                            for _, msg in ui.messages))


if __name__ == "__main__":
    unittest.main()
