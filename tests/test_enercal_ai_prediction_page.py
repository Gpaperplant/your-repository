"""Offline Streamlit page tests for verified real-case AI predictions."""

import importlib.util
import sys
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType

import pytest


PAGE = Path(__file__).resolve().parents[1] / "enercal" / "enercal_ai_prediction.py"


class Stopped(Exception):
    pass


class FakeStreamlit(ModuleType):
    def __init__(self, role="Requester", *, feature_count=49, click=False):
        super().__init__("streamlit")
        self.session_state = {"role": role}
        self.feature_count = feature_count
        self.click = click
        self.messages = []
        self.metrics = []
        self.selects = []
        self.buttons = []

    def stop(self):
        raise Stopped()

    def subheader(self, message):
        self.messages.append(("subheader", str(message)))

    def caption(self, message):
        self.messages.append(("caption", str(message)))

    def write(self, message):
        self.messages.append(("write", str(message)))

    def info(self, message):
        self.messages.append(("info", str(message)))

    def error(self, message):
        self.messages.append(("error", str(message)))

    def warning(self, message):
        self.messages.append(("warning", str(message)))

    def columns(self, count):
        return [self] * count

    def metric(self, label, value):
        self.metrics.append((label, value))

    def selectbox(self, label, options, **kwargs):
        options = list(options)
        self.selects.append((label, options))
        if label == "Prediction model":
            return self.feature_count
        return options[0]

    def button(self, label, **kwargs):
        self.buttons.append(label)
        return self.click

    def spinner(self, message):
        return nullcontext()


def sample_prediction(count=49, warning=None):
    result = {
        "case_ref": "MOD1001", "elevator_ref": "LIFT-1", "feature_count": count,
        "mod_estimated_savings_percent": 52.69,
        "predicted_savings_gap_pp": -3.5,
        "predicted_iso_savings_percent": 49.19,
        "evaluation_mae_pp": 4.1, "trained_case_count": 32,
        "training_fingerprint": "f" * 64,
    }
    if warning:
        result["warning"] = warning
    return result


def import_page(monkeypatch, *, role="Requester", count=49, click=False,
                rows=None, read_error=None, prediction_error=None, warning=None):
    st = FakeStreamlit(role, feature_count=count, click=click)
    rows = [{"case_ref": "MOD1001", "elevator_ref": "LIFT-1"}] if rows is None else rows
    calls = {"read": [], "predict": []}

    dataset = ModuleType("services.ai_dataset")

    def read_dataset(feature_count, *, training):
        calls["read"].append((feature_count, training))
        if read_error:
            raise read_error
        return rows

    dataset.load_ai_dataset = read_dataset
    prediction = ModuleType("services.ai_prediction")
    prediction.MODEL_FILES = {26: "ridge_26.joblib", 49: "ridge_49.joblib"}

    def predict(case_ref, directory, *, feature_count):
        calls["predict"].append((case_ref, Path(directory), feature_count))
        if prediction_error:
            raise prediction_error
        return sample_prediction(feature_count, warning=warning)

    prediction.predict_case_from_database = predict

    with monkeypatch.context() as scope:
        scope.setitem(sys.modules, "streamlit", st)
        scope.setitem(sys.modules, "services.ai_dataset", dataset)
        scope.setitem(sys.modules, "services.ai_prediction", prediction)
        spec = importlib.util.spec_from_file_location("_fyp_prediction_page_test", PAGE)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Stopped:
            if role == "Admin":
                raise
    return module, st, calls


def saved_folder(root, name="run1", *, count=49):
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "evaluation.json").write_text("{}")
    (path / f"ridge_{count}.joblib").write_bytes(b"test-placeholder")
    return path


def render_admin(module, st, root, *, count=None):
    st.session_state["role"] = "Admin"
    st.messages.clear()
    st.metrics.clear()
    st.selects.clear()
    st.buttons.clear()
    if count is not None:
        st.feature_count = count
    module.MODEL_PARENT = root
    module.render_prediction_page()


def get_messages(st, kind):
    return " | ".join(msg for channel, msg in st.messages if channel == kind)


def test_page_exists_and_compiles():
    assert PAGE.is_file()
    compile(PAGE.read_text(), str(PAGE), "exec")


@pytest.mark.parametrize("role", ["Requester", "Responder", None, "", "Engineer"])
def test_non_admin_blocked_before_data_or_model(monkeypatch, role):
    _, st, calls = import_page(monkeypatch, role=role, click=True)
    assert "Admin access is required" in get_messages(st, "error")
    assert calls == {"read": [], "predict": []}
    assert st.selects == []


def test_admin_no_saved_model_does_not_read_database(monkeypatch, tmp_path):
    module, st, calls = import_page(monkeypatch)
    render_admin(module, st, tmp_path)
    assert "No saved compatible model" in get_messages(st, "info")
    assert calls == {"read": [], "predict": []}


@pytest.mark.parametrize("count", [26, 49])
def test_discovery_filters_by_feature_count(monkeypatch, tmp_path, count):
    module, _, _ = import_page(monkeypatch)
    first = saved_folder(tmp_path, "first", count=26)
    second = saved_folder(tmp_path, "second", count=49)
    assert module.available_model_folders(tmp_path, count) == [first if count == 26 else second]


def test_discovery_nonexistent_root(monkeypatch, tmp_path):
    module, _, _ = import_page(monkeypatch)
    assert module.available_model_folders(tmp_path / "missing", 49) == []


@pytest.mark.parametrize("count", [0, 25, 27, 50, None])
def test_discovery_rejects_unsupported_feature_count(monkeypatch, tmp_path, count):
    module, _, _ = import_page(monkeypatch)
    with pytest.raises(ValueError, match="26 or 49"):
        module.available_model_folders(tmp_path, count)


def test_discovery_ignores_incomplete_runs(monkeypatch, tmp_path):
    module, _, _ = import_page(monkeypatch)
    (tmp_path / "empty").mkdir()
    (tmp_path / "file.txt").write_text("hi")
    assert module.available_model_folders(tmp_path, 49) == []


def test_discovery_ignores_symlinked_folder(monkeypatch, tmp_path):
    module, _, _ = import_page(monkeypatch)
    real = saved_folder(tmp_path, "valid")
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)
    assert module.available_model_folders(tmp_path, 49) == [real]


def test_discovery_ignores_symlinked_model_file(monkeypatch, tmp_path):
    module, _, _ = import_page(monkeypatch)
    target = saved_folder(tmp_path, "target") / "ridge_49.joblib"
    linked_folder = tmp_path / "linked_model"
    linked_folder.mkdir()
    (linked_folder / "evaluation.json").write_text("{}")
    (linked_folder / "ridge_49.joblib").symlink_to(target)
    assert [folder.name for folder in module.available_model_folders(tmp_path, 49)] == ["target"]


def test_discovery_ignores_symlinked_metadata(monkeypatch, tmp_path):
    module, _, _ = import_page(monkeypatch)
    target = saved_folder(tmp_path, "target") / "evaluation.json"
    linked_folder = tmp_path / "linked_meta"
    linked_folder.mkdir()
    (linked_folder / "ridge_49.joblib").write_bytes(b"content")
    (linked_folder / "evaluation.json").symlink_to(target)
    assert [folder.name for folder in module.available_model_folders(tmp_path, 49)] == ["target"]


def test_discovery_latest_folder_name_first(monkeypatch, tmp_path):
    module, _, _ = import_page(monkeypatch)
    saved_folder(tmp_path, "20260101")
    saved_folder(tmp_path, "20261201")
    assert [x.name for x in module.available_model_folders(tmp_path, 49)] == ["20261201", "20260101"]


def test_eligible_cases_are_sorted(monkeypatch):
    rows = [
        {"case_ref": "MOD-B", "elevator_ref": "LIFT-2"},
        {"case_ref": "MOD-A", "elevator_ref": "LIFT-1"},
    ]
    module, _, calls = import_page(monkeypatch, rows=rows)
    assert list(module.eligible_cases(26)) == ["MOD-A", "MOD-B"]
    assert calls["read"] == [(26, False)]


def test_duplicate_case_reference_is_rejected(monkeypatch):
    rows = [{"case_ref": "X", "elevator_ref": "A"}, {"case_ref": "X", "elevator_ref": "B"}]
    module, _, _ = import_page(monkeypatch, rows=rows)
    with pytest.raises(ValueError, match="Duplicate eligible"):
        module.eligible_cases(49)


def test_no_eligible_cases_never_predicts(monkeypatch, tmp_path):
    module, st, calls = import_page(monkeypatch, click=True, rows=[])
    saved_folder(tmp_path)
    render_admin(module, st, tmp_path)
    assert "No fully verified real cases" in get_messages(st, "info")
    assert calls["read"] == [(49, False)]
    assert calls["predict"] == []
    assert st.buttons == []


def test_database_error_is_displayed_without_prediction(monkeypatch, tmp_path):
    module, st, calls = import_page(monkeypatch, click=True, read_error=RuntimeError("db offline"))
    saved_folder(tmp_path)
    render_admin(module, st, tmp_path)
    assert "db offline" in get_messages(st, "error")
    assert calls["predict"] == []


@pytest.mark.parametrize("count", [26, 49])
def test_not_predicting_until_explicit_click(monkeypatch, tmp_path, count):
    module, st, calls = import_page(monkeypatch, count=count, click=False)
    saved_folder(tmp_path, count=count)
    render_admin(module, st, tmp_path)
    assert calls["read"] == [(count, False)]
    assert calls["predict"] == []
    assert st.buttons == ["Predict ISO energy savings"]
    assert "ISO After" in get_messages(st, "caption")


@pytest.mark.parametrize("count", [26, 49])
def test_prediction_uses_selected_case_model_and_count(monkeypatch, tmp_path, count):
    module, st, calls = import_page(monkeypatch, count=count, click=True)
    folder = saved_folder(tmp_path, count=count)
    render_admin(module, st, tmp_path)
    assert calls["predict"] == [("MOD1001", folder, count)]
    assert ("Predicted ISO saving", "49.19%") in st.metrics
    assert ("Predicted savings gap", "-3.50 pp") in st.metrics
    assert "not an ISO test measurement" in get_messages(st, "caption")
    assert "f" * 64 in get_messages(st, "caption")


def test_prediction_error_shows_message(monkeypatch, tmp_path):
    module, st, calls = import_page(monkeypatch, click=True, prediction_error=ValueError("incompatible version"))
    saved_folder(tmp_path)
    render_admin(module, st, tmp_path)
    assert calls["predict"]
    assert "incompatible version" in get_messages(st, "error")
    assert not st.metrics


def test_prediction_warning_is_visible(monkeypatch, tmp_path):
    module, st, _ = import_page(monkeypatch, click=True, warning="Outside physical range")
    saved_folder(tmp_path)
    render_admin(module, st, tmp_path)
    assert "Outside physical range" in get_messages(st, "warning")


def test_page_does_not_allow_manual_model_path_or_feature_values(monkeypatch, tmp_path):
    module, st, _ = import_page(monkeypatch)
    saved_folder(tmp_path)
    render_admin(module, st, tmp_path)
    assert [label for label, _ in st.selects] == [
        "Prediction model", "Saved model run", "Approved modernisation case"
    ]
    assert "manual or synthetic inputs" in get_messages(st, "caption")


def test_empty_26_models_block_26_prediction(monkeypatch, tmp_path):
    module, st, calls = import_page(monkeypatch, count=26, click=True)
    saved_folder(tmp_path, count=49)
    render_admin(module, st, tmp_path)
    assert "No saved compatible model" in get_messages(st, "info")
    assert calls == {"read": [], "predict": []}


def test_result_labels_display_correct_units(monkeypatch):
    module, st, _ = import_page(monkeypatch)
    module.show_prediction(sample_prediction(26))
    assert ("MOD estimated saving", "52.69%") in st.metrics
    assert ("Predicted savings gap", "-3.50 pp") in st.metrics
    assert ("Predicted ISO saving", "49.19%") in st.metrics
    assert "Held-out model MAE" in get_messages(st, "caption")
