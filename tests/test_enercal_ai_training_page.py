"""Offline Streamlit regression tests for the real-evidence AI training page."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

PAGE = Path(__file__).resolve().parents[1] / "enercal" / "enercal_ai_training.py"


class PageStopped(Exception):
    pass


class FakeStreamlit(ModuleType):
    def __init__(self, role="Admin", *, run=False, save=False, state=None):
        super().__init__("streamlit")
        self.session_state = state if state is not None else {"role": role}
        self.session_state.setdefault("role", role)
        self.run = run
        self.save = save
        self.messages = []
        self.metrics = []
        self.tables = []
        self.forms = []
        self.buttons = []
        self.code_blocks = []
        self.slider_value = 0.20
        self.seed_value = 42
        self.alpha_value = 10.0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stop(self):
        raise PageStopped()

    def subheader(self, value):
        self.messages.append(("subheader", str(value)))

    def caption(self, value):
        self.messages.append(("caption", str(value)))

    def write(self, value):
        self.messages.append(("write", str(value)))

    def info(self, value):
        self.messages.append(("info", str(value)))

    def error(self, value):
        self.messages.append(("error", str(value)))

    def success(self, value):
        self.messages.append(("success", str(value)))

    def dataframe(self, value, **kwargs):
        self.tables.append(value)

    def columns(self, count):
        return [self] * count

    def metric(self, label, value):
        self.metrics.append((label, value))

    def form(self, key):
        self.forms.append(key)
        return self

    def slider(self, label, **kwargs):
        return self.slider_value

    def number_input(self, label, **kwargs):
        return self.seed_value if label == "Random seed" else self.alpha_value

    def form_submit_button(self, label, **kwargs):
        return self.run

    def button(self, label, **kwargs):
        self.buttons.append(label)
        return self.save

    def code(self, value):
        self.code_blocks.append(str(value))


def records(count, elevators=8, *, start=1):
    return [
        {"case_id": i, "elevator_ref": f"LIFT{i % elevators}", "version": 1}
        for i in range(start, start + count)
    ]


def sample_result():
    return {
        "baseline_metrics": {"mae_pp": 6.0, "rmse_pp": 7.0, "r2": -0.2},
        "metrics": {
            26: {"mae_pp": 4.0, "rmse_pp": 5.0, "r2": 0.3},
            49: {"mae_pp": 3.0, "rmse_pp": 4.0, "r2": 0.5},
        },
        "matched_case_count": 24,
        "excluded_mod_only_case_count": 0,
        "train_case_ids": list(range(1, 19)),
        "test_case_ids": list(range(19, 25)),
        "random_state": 42,
        "test_size": 0.2,
        "ridge_alpha": 10.0,
    }


def load_page(monkeypatch, *, role="Admin", mod=None, combined=None,
              run=False, save=False, state=None, load_error=None,
              train_error=None, save_error=None):
    streamlit = FakeStreamlit(role, run=run, save=save, state=state)
    mod = [] if mod is None else mod
    combined = [] if combined is None else combined
    calls = {"load": [], "train": [], "save": []}

    dataset = ModuleType("services.ai_dataset")

    def load_dataset(feature_count, *, training):
        calls["load"].append((feature_count, training))
        if load_error:
            raise load_error
        return mod if feature_count == 26 else combined

    dataset.load_ai_dataset = load_dataset
    dataset.dataset_fingerprint = lambda rows, count, *, training: (
        str((count, training, tuple((r["case_id"], r.get("version")) for r in rows)))
    )

    training = ModuleType("services.ai_training")
    training.MIN_CASES = 12
    training.MIN_ELEVATORS = 6

    def train_models(left, right, **kwargs):
        calls["train"].append((left, right, kwargs))
        if train_error:
            raise train_error
        return sample_result()

    def save_models(result, output):
        calls["save"].append((result, Path(output)))
        if save_error:
            raise save_error
        return {
            "model_26": str(output / "ridge_26.joblib"),
            "model_49": str(output / "ridge_49.joblib"),
            "evaluation": str(output / "evaluation.json"),
        }

    training.train_model_comparison = train_models
    training.save_training_artifacts = save_models

    with monkeypatch.context() as scope:
        scope.setitem(sys.modules, "streamlit", streamlit)
        scope.setitem(sys.modules, "services.ai_dataset", dataset)
        scope.setitem(sys.modules, "services.ai_training", training)
        spec = importlib.util.spec_from_file_location(
            "_fyp_training_page_under_test", PAGE
        )
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except PageStopped:
            if role == "Admin":
                raise
    return module, streamlit, calls


def message_text(st, category):
    return " | ".join(msg for kind, msg in st.messages if kind == category)


def test_page_exists_and_compiles():
    assert PAGE.is_file()
    compile(PAGE.read_text(), str(PAGE), "exec")


@pytest.mark.parametrize("role", ["Requester", "Responder", None, "", "Engineer"])
def test_non_admin_is_blocked_before_loading_db(monkeypatch, role):
    _, st, calls = load_page(monkeypatch, role=role)
    assert "Admin access" in message_text(st, "error")
    assert calls["load"] == []
    assert calls["train"] == []


def test_admin_reads_only_real_training_views(monkeypatch):
    _, st, calls = load_page(monkeypatch)
    assert calls["load"] == [(26, True), (49, True)]
    assert "AI model training" in message_text(st, "subheader")
    assert "ISO After" in message_text(st, "caption")
    assert not calls["train"]


def test_empty_views_show_message_without_training(monkeypatch):
    _, st, calls = load_page(monkeypatch, run=True)
    assert "at least 12" in message_text(st, "info")
    assert "560-row synthetic" in message_text(st, "caption")
    assert calls["train"] == []
    assert st.forms == []


@pytest.mark.parametrize("mod_count,full_count", [(50, 0), (50, 11), (11, 11)])
def test_not_enough_matched_cases_cannot_train(monkeypatch, mod_count, full_count):
    _, st, calls = load_page(
        monkeypatch, mod=records(mod_count), combined=records(full_count), run=True
    )
    assert "at least" in message_text(st, "info")
    assert calls["train"] == []


def test_not_enough_elevators_cannot_train(monkeypatch):
    _, st, calls = load_page(
        monkeypatch, mod=records(24, 5), combined=records(24, 5), run=True
    )
    assert ("Matched elevators", 5) in st.metrics
    assert calls["train"] == []


def test_loader_error_is_displayed_and_prevents_training(monkeypatch):
    _, st, calls = load_page(monkeypatch, run=True, load_error=RuntimeError("view unavailable"))
    assert "view unavailable" in message_text(st, "error")
    assert calls["train"] == []
    assert st.forms == []


def test_eligibility_counts_match_by_case_id(monkeypatch):
    module, _, _ = load_page(monkeypatch)
    summary = module.eligible_summary(records(24), records(18, start=7))
    assert summary == {
        "mod_cases": 24,
        "combined_cases": 18,
        "matched_cases": 18,
        "matched_elevators": 8,
        "ready": True,
    }


def test_unmatched_combined_rows_do_not_count_towards_minimum(monkeypatch):
    module, _, _ = load_page(monkeypatch)
    summary = module.eligible_summary(records(4), records(24))
    assert summary["matched_cases"] == 4
    assert not summary["ready"]


def test_training_is_explicit_button_action(monkeypatch):
    _, st, calls = load_page(
        monkeypatch, mod=records(24), combined=records(24), run=False
    )
    assert st.forms == ["fyp_ai_training_form"]
    assert calls["train"] == []
    assert not st.tables


def test_train_uses_same_loaded_rows_and_selected_parameters(monkeypatch):
    mod, combined = records(24), records(24)
    _, st, calls = load_page(monkeypatch, mod=mod, combined=combined, run=True)
    assert len(calls["train"]) == 1
    first, second, kwargs = calls["train"][0]
    assert first is mod and second is combined
    assert kwargs == {"test_size": 0.2, "random_state": 42, "alpha": 10.0}
    assert "trained and evaluated" in message_text(st, "success")
    assert len(st.tables) == 1
    assert [r["Model"] for r in st.tables[0]] == [
        "Mean baseline", "Ridge · 26 MOD features", "Ridge · 49 MOD + ISO Before features"
    ]
    assert calls["save"] == []


def test_result_table_exposes_mae_rmse_and_r2(monkeypatch):
    _, st, _ = load_page(
        monkeypatch, mod=records(24), combined=records(24), run=True
    )
    assert list(st.tables[0][0]) == ["Model", "MAE (pp)", "RMSE (pp)", "R²"]
    assert st.tables[0][2]["MAE (pp)"] == 3.0
    assert "same held-out cases" in message_text(st, "caption")


def test_train_failure_does_not_offer_model_save(monkeypatch):
    _, st, calls = load_page(
        monkeypatch, mod=records(24), combined=records(24),
        run=True, save=True, train_error=ValueError("Grouped split failed"),
    )
    assert "Grouped split failed" in message_text(st, "error")
    assert "Save trained model files" not in st.buttons
    assert calls["save"] == []


def test_result_persists_without_retraining_on_rerun(monkeypatch):
    mod, combined, state = records(24), records(24), {"role": "Admin"}
    load_page(monkeypatch, mod=mod, combined=combined, run=True, state=state)
    _, st, calls = load_page(
        monkeypatch, mod=mod, combined=combined, run=False, state=state
    )
    assert calls["train"] == []
    assert st.tables
    assert "Save trained model files" in st.buttons


def test_source_change_invalidates_old_model(monkeypatch):
    mod, combined, state = records(24), records(24), {"role": "Admin"}
    load_page(monkeypatch, mod=mod, combined=combined, run=True, state=state)
    assert "fyp_ai_training_result" in state
    modified = records(24)
    modified[0]["version"] = 2
    _, st, calls = load_page(
        monkeypatch, mod=mod, combined=modified, run=False, state=state
    )
    assert "fyp_ai_training_result" not in state
    assert calls["train"] == []
    assert not st.tables


def test_model_export_only_after_explicit_click(monkeypatch, tmp_path):
    mod, combined, state = records(24), records(24), {"role": "Admin"}
    load_page(monkeypatch, mod=mod, combined=combined, run=True, state=state)
    module, st, calls = load_page(
        monkeypatch, mod=mod, combined=combined, save=False, state=state
    )
    module.MODEL_PARENT = tmp_path / "models"
    st.save = True
    module.render_training_page()
    assert len(calls["save"]) == 1
    result, dest = calls["save"][0]
    assert result == state["fyp_ai_training_result"]
    assert dest.parent == tmp_path / "models"
    assert dest.name.startswith("real_evidence_")
    assert len(state["fyp_ai_training_artifacts"]) == 3
    assert "ridge_26.joblib" in st.code_blocks[-1]


def test_save_error_is_reported_without_success(monkeypatch, tmp_path):
    mod, combined, state = records(24), records(24), {"role": "Admin"}
    load_page(monkeypatch, mod=mod, combined=combined, run=True, state=state)
    module, st, calls = load_page(
        monkeypatch, mod=mod, combined=combined, save=False, state=state,
        save_error=OSError("disk full"),
    )
    module.MODEL_PARENT = tmp_path / "models"
    st.save = True
    module.render_training_page()
    assert calls["save"]
    assert "disk full" in message_text(st, "error")
    assert "fyp_ai_training_artifacts" not in state


def test_result_displays_saved_file_paths_on_rerun(monkeypatch):
    mod, combined, state = records(24), records(24), {"role": "Admin"}
    load_page(monkeypatch, mod=mod, combined=combined, run=True, state=state)
    state["fyp_ai_training_artifacts"] = {"evaluation": "/models/sample/evaluation.json"}
    _, st, _ = load_page(
        monkeypatch, mod=mod, combined=combined, run=False, state=state
    )
    assert st.code_blocks == ["/models/sample/evaluation.json"]


def test_retraining_discards_previous_saved_path(monkeypatch):
    mod, combined, state = records(24), records(24), {"role": "Admin"}
    load_page(monkeypatch, mod=mod, combined=combined, run=True, state=state)
    state["fyp_ai_training_artifacts"] = {"evaluation": "/old/evaluation.json"}
    _, st, _ = load_page(
        monkeypatch, mod=mod, combined=combined, run=True, state=state
    )
    assert "fyp_ai_training_artifacts" not in state
    assert not st.code_blocks


def test_page_does_not_import_synthetic_workbook_loader():
    code = PAGE.read_text()
    assert "KONE_AI_Training_Predict_Savings_Gap_560" not in code
    assert "load_ai_dataset(26, training=True)" in code
    assert "load_ai_dataset(49, training=True)" in code


def test_page_not_connected_to_menu_without_navigation_change():
    assert PAGE.parent.name == "enercal"
    assert PAGE.name == "enercal_ai_training.py"
