"""Role and sidebar checks for the consolidated AI dashboard."""

import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

APP = Path(__file__).resolve().parents[1] / "streamlit_app.py"
AI_PAGE = "enercal/enercal_ai_engineering12.py"


class State(dict):
    def __getattr__(self, key):
        return self[key]

    def __setattr__(self, key, value):
        self[key] = value


class FakeStreamlit(ModuleType):
    def __init__(self, role):
        super().__init__("streamlit")
        self.session_state = State(role=role)
        self.sidebar = SimpleNamespace(caption=lambda *_: None)
        self.pages = None
        self.runs = 0
        self.config = None
        self.logo_args = None
        self.reruns = 0
        self.login_selection = None
        self.login_submitted = False

    def set_page_config(self, **kwargs):
        self.config = kwargs

    def logo(self, *args, **kwargs):
        self.logo_args = (args, kwargs)

    def Page(self, source, **kwargs):
        return {"source": source, **kwargs}

    def navigation(self, pages):
        self.pages = pages
        return SimpleNamespace(run=lambda: setattr(self, "runs", self.runs + 1))

    def subheader(self, *_):
        pass

    def caption(self, *_):
        pass

    def selectbox(self, *_args, **_kwargs):
        return self.login_selection

    def button(self, *_args, **_kwargs):
        return self.login_submitted

    def rerun(self):
        self.reruns += 1


def app(role):
    fake = FakeStreamlit(role)
    with patch.dict(sys.modules, {"streamlit": fake}):
        result = runpy.run_path(str(APP))
    return fake, result


def paths(pages):
    return [p["source"] for group in pages.values() for p in group]


def test_only_one_ai_page_visible():
    st, _ = app("Admin")
    visible = paths(st.pages)
    assert visible.count(AI_PAGE) == 1
    assert "enercal/enercal_ai_evaluation.py" not in visible
    assert "enercal/enercal_ai_prediction.py" not in visible
    assert "enercal/enercal_ai_training.py" not in visible
    assert next(p for p in st.pages["EnerCal"] if p["source"] == AI_PAGE)["title"] == "AI Dashboard"


def test_four_existing_enercal_pages_unchanged():
    st, _ = app("Admin")
    assert [p["source"] for p in st.pages["EnerCal"][:4]] == [
        "enercal/enercal_upload.py",
        "enercal/enercal_measurements.py",
        "enercal/enercal_review.py",
        "enercal/enercal_reporting.py",
    ]
    assert st.pages["EnerCal"][0]["default"] is True
    assert list(st.pages) == ["Requests", "Responses", "EnerCal", "Account"]


@pytest.mark.parametrize("role", ["Requester", "Responder"])
def test_non_admin_cannot_see_ai(role):
    st, _ = app(role)
    assert "EnerCal" not in st.pages
    assert AI_PAGE not in paths(st.pages)


@pytest.mark.parametrize("role", [None, "", "Other"])
def test_invalid_role_shows_login(role):
    st, _ = app(role)
    assert len(st.pages) == 1
    assert st.pages[0]["title"] == "Choose role"


def test_logout_preserves_existing_functionality():
    st, funcs = app("Admin")
    funcs["logout"]()
    assert st.session_state.role is None
    assert st.reruns == 1


def test_existing_legacy_files_preserved():
    for file in ("enercal_ai_evaluation.py", "enercal_ai_training.py", "enercal_ai_prediction.py"):
        assert (APP.parent / "enercal" / file).exists()
