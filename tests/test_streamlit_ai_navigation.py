"""Offline navigation checks for the AI training page."""

import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest


APP = Path(__file__).resolve().parents[1] / "streamlit_app.py"
AI_PAGE = "enercal/enercal_ai_training.py"
ORIGINAL_ENERCAL_PAGES = (
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
        self.page_config = None
        self.logo_config = None
        self.navigation_entries = None
        self.navigation_runs = 0
        self.reruns = 0
        self.login_choice = None
        self.login_submit = False
        self.login_button_disabled = None
        self.headings = []

    def set_page_config(self, **kwargs):
        self.page_config = kwargs

    def logo(self, *args, **kwargs):
        self.logo_config = (args, kwargs)

    def Page(self, source, **kwargs):
        return {"source": source, **kwargs}

    def navigation(self, entries):
        self.navigation_entries = entries
        return SimpleNamespace(run=self._run_navigation)

    def _run_navigation(self):
        self.navigation_runs += 1

    def subheader(self, title):
        self.headings.append(title)

    def caption(self, *args, **kwargs):
        pass

    def selectbox(self, *args, **kwargs):
        return self.login_choice

    def button(self, *args, **kwargs):
        self.login_button_disabled = kwargs.get("disabled")
        return self.login_submit

    def rerun(self):
        self.reruns += 1


def open_navigation(role=None):
    fake = FakeStreamlit(role)
    with patch.dict(sys.modules, {"streamlit": fake}):
        namespace = runpy.run_path(str(APP))
    return fake, namespace


def page_paths(pages):
    return [item["source"] for group in pages.values() for item in group]


def test_admin_sees_training_page():
    st, _ = open_navigation("Admin")
    assert AI_PAGE in page_paths(st.navigation_entries)


def test_admin_keeps_four_existing_enercal_pages_in_order():
    st, _ = open_navigation("Admin")
    paths = [p["source"] for p in st.navigation_entries["EnerCal"]]
    assert tuple(paths[:4]) == ORIGINAL_ENERCAL_PAGES
    assert paths[-1] == AI_PAGE


def test_training_page_title_and_icon():
    st, _ = open_navigation("Admin")
    page = st.navigation_entries["EnerCal"][-1]
    assert page["title"] == "AI Model Training"
    assert page["icon"] == ":material/model_training:"


def test_training_page_is_not_default():
    st, _ = open_navigation("Admin")
    pages = st.navigation_entries["EnerCal"]
    assert pages[0]["default"] is True
    assert pages[-1].get("default") is not True


def test_admin_keeps_request_response_and_account_sections():
    st, _ = open_navigation("Admin")
    assert list(st.navigation_entries) == ["Requests", "Responses", "EnerCal", "Account"]


@pytest.mark.parametrize("role", ["Requester", "Responder"])
def test_other_roles_cannot_navigate_to_training(role):
    st, _ = open_navigation(role)
    assert AI_PAGE not in page_paths(st.navigation_entries)
    assert "EnerCal" not in st.navigation_entries


def test_requester_default_unchanged():
    st, _ = open_navigation("Requester")
    assert st.navigation_entries["Requests"][0]["default"] is True
    assert list(st.navigation_entries) == ["Requests", "Account"]


def test_responder_default_unchanged():
    st, _ = open_navigation("Responder")
    assert st.navigation_entries["Responses"][0]["default"] is True
    assert list(st.navigation_entries) == ["Responses", "Account"]


@pytest.mark.parametrize("role", [None, "Unknown", ""])
def test_unrecognised_role_shows_login_only(role):
    st, _ = open_navigation(role)
    assert len(st.navigation_entries) == 1
    assert st.navigation_entries[0]["title"] == "Choose role"


def test_login_requires_role_selection():
    st, module = open_navigation()
    with patch.dict(sys.modules, {"streamlit": st}):
        module["login"]()
    assert st.login_button_disabled is True
    assert st.session_state.role is None


def test_login_updates_role_and_requests_rerun():
    st, module = open_navigation()
    st.login_choice = "Admin"
    st.login_submit = True
    with patch.dict(sys.modules, {"streamlit": st}):
        module["login"]()
    assert st.login_button_disabled is False
    assert st.session_state.role == "Admin"
    assert st.reruns == 1


def test_logout_clears_role_and_requests_rerun():
    st, module = open_navigation("Admin")
    module["logout"]()
    assert st.session_state.role is None
    assert st.reruns == 1


def test_navigation_runs_exactly_once():
    st, _ = open_navigation("Admin")
    assert st.navigation_runs == 1
    assert st.sidebar_notes == ["Current role  Admin"]


def test_app_metadata_unchanged():
    st, _ = open_navigation("Admin")
    assert st.page_config["page_title"] == "KONE Modernisation"
    assert st.page_config["layout"] == "wide"
    assert st.logo_config[0] == ("images/horizontal_blue.png",)


def test_training_page_file_exists():
    assert (APP.parent / AI_PAGE).is_file()