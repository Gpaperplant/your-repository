"""Offline navigation checks for the AI savings prediction page."""

import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest


APP = Path(__file__).resolve().parents[1] / "streamlit_app.py"
PREDICTION = "enercal/enercal_ai_prediction.py"
TRAINING = "enercal/enercal_ai_training.py"
EXISTING_PAGES = (
    "enercal/enercal_upload.py",
    "enercal/enercal_measurements.py",
    "enercal/enercal_review.py",
    "enercal/enercal_reporting.py",
)


class SessionState(dict):
    def __getattr__(self, name):
        return self[name]

    def __setattr__(self, name, value):
        self[name] = value


class FakeStreamlit(ModuleType):
    def __init__(self, role=None):
        super().__init__("streamlit")
        self.session_state = SessionState(role=role)
        self.sidebar_notes = []
        self.sidebar = SimpleNamespace(caption=self.sidebar_notes.append)
        self.pages = None
        self.runs = 0
        self.config = None
        self.logo_args = None
        self.login_selection = None
        self.login_submitted = False
        self.reruns = 0

    def set_page_config(self, **kwargs):
        self.config = kwargs

    def logo(self, *args, **kwargs):
        self.logo_args = (args, kwargs)

    def Page(self, source, **kwargs):
        return {"source": source, **kwargs}

    def navigation(self, entries):
        self.pages = entries
        return SimpleNamespace(run=self._run)

    def _run(self):
        self.runs += 1

    def subheader(self, *args, **kwargs):
        pass

    def caption(self, *args, **kwargs):
        pass

    def selectbox(self, *args, **kwargs):
        return self.login_selection

    def button(self, *args, **kwargs):
        return self.login_submitted

    def rerun(self):
        self.reruns += 1


def load_app(role):
    fake = FakeStreamlit(role)
    with patch.dict(sys.modules, {"streamlit": fake}):
        app = runpy.run_path(str(APP))
    return fake, app


def all_paths(groups):
    if isinstance(groups, list):
        return [p["source"] for p in groups]
    return [p["source"] for group in groups.values() for p in group]


def test_prediction_page_is_present_for_admin():
    fake, _ = load_app("Admin")
    assert PREDICTION in all_paths(fake.pages)


def test_prediction_is_in_enercal_section():
    fake, _ = load_app("Admin")
    assert PREDICTION in [p["source"] for p in fake.pages["EnerCal"]]


def test_existing_enercal_pages_remain_first_and_ordered():
    fake, _ = load_app("Admin")
    assert tuple(p["source"] for p in fake.pages["EnerCal"][:4]) == EXISTING_PAGES


def test_training_and_prediction_are_adjacent():
    fake, _ = load_app("Admin")
    paths = [p["source"] for p in fake.pages["EnerCal"]]
    assert paths[-2:] == [PREDICTION, TRAINING]


def test_prediction_page_title_and_icon():
    fake, _ = load_app("Admin")
    page = next(p for p in fake.pages["EnerCal"] if p["source"] == PREDICTION)
    assert page["title"] == "AI Savings Prediction"
    assert page["icon"] == ":material/online_prediction:"


def test_prediction_not_default():
    fake, _ = load_app("Admin")
    page = next(p for p in fake.pages["EnerCal"] if p["source"] == PREDICTION)
    assert page.get("default") is not True


def test_enercal_upload_stays_default():
    fake, _ = load_app("Admin")
    defaults = [p["source"] for p in fake.pages["EnerCal"] if p.get("default")]
    assert defaults == ["enercal/enercal_upload.py"]


def test_no_duplicate_pages():
    fake, _ = load_app("Admin")
    paths = [p for p in all_paths(fake.pages) if isinstance(p, str)]
    assert len(paths) == len(set(paths))


def test_admin_section_names_unchanged():
    fake, _ = load_app("Admin")
    assert list(fake.pages) == ["Requests", "Responses", "EnerCal", "Account"]


@pytest.mark.parametrize("role", ["Requester", "Responder"])
def test_non_admin_roles_cannot_see_prediction(role):
    fake, _ = load_app(role)
    assert "EnerCal" not in fake.pages
    assert PREDICTION not in all_paths(fake.pages)


@pytest.mark.parametrize("role", [None, "", "Unauthorized"])
def test_invalid_role_only_shows_login(role):
    fake, _ = load_app(role)
    assert len(fake.pages) == 1
    assert fake.pages[0]["title"] == "Choose role"
    assert PREDICTION not in all_paths(fake.pages)


def test_requester_and_responder_section_order_unchanged():
    requester, _ = load_app("Requester")
    responder, _ = load_app("Responder")
    assert list(requester.pages) == ["Requests", "Account"]
    assert list(responder.pages) == ["Responses", "Account"]


def test_admin_page_file_exists():
    assert (APP.parent / PREDICTION).is_file()


def test_training_page_file_still_exists():
    assert (APP.parent / TRAINING).is_file()


def test_app_runs_navigation_once():
    fake, _ = load_app("Admin")
    assert fake.runs == 1


def test_admin_sidebar_role_caption_unchanged():
    fake, _ = load_app("Admin")
    assert fake.sidebar_notes == ["Current role  Admin"]


def test_app_title_layout_logo_unchanged():
    fake, _ = load_app("Admin")
    assert fake.config["page_title"] == "KONE Modernisation"
    assert fake.config["layout"] == "wide"
    assert fake.logo_args[0] == ("images/horizontal_blue.png",)


def test_login_and_logout_callbacks_unchanged():
    fake, app = load_app("Admin")
    app["logout"]()
    assert fake.session_state.role is None
    assert fake.reruns == 1
