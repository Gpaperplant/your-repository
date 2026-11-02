"""Shared presentation helpers. This module never changes business data."""

from html import escape
import streamlit as st


def apply_theme():
    """A small, consistent corporate theme using Streamlit's native widgets."""
    st.markdown(
        """
        <style>
        :root {
            --kone-blue: #0067B9;
            --kone-deep: #073763;
            --kone-muted: #60768B;
            --kone-line: #DDE7EF;
            --kone-surface: #F5F8FC;
        }
        .stApp { background-color: #F5F8FC; }
        [data-testid="stSidebar"] { background: #FFFFFF; border-right: 1px solid #DDE7EF; }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p { line-height: 1.5; }
        .block-container {
            max-width: 1500px;
            padding-top: 2.1rem;
            padding-bottom: 3rem;
        }
        h1, h2, h3 { letter-spacing: -0.025em; color: #123554; }
        h1 { font-weight: 740; }
        h3 { font-weight: 680; }
        [data-testid="stMetric"] {
            border: 1px solid #DDE7EF;
            background: white;
            padding: 1.1rem 1.2rem;
            border-radius: 12px;
            min-height: 114px;
        }
        [data-testid="stMetricLabel"] { color: #4D657B; }
        [data-testid="stMetricValue"] { color: #0B4777; }
        [data-testid="stForm"] {
            background-color: #FFFFFF;
            border: 1px solid #DDE7EF;
            border-radius: 12px;
            padding: 1.15rem;
        }
        div[data-testid="stVerticalBlockBorderWrapper"] { border-color: #DDE7EF; }
        div.stButton > button[kind="primary"],
        div.stFormSubmitButton > button[kind="primary"] {
            background: #0067B9;
            border-color: #0067B9;
        }
        .kone-eyebrow {
            color: #0067B9;
            font-size: 0.73rem;
            letter-spacing: 0.11em;
            text-transform: uppercase;
            font-weight: 750;
            margin: 0 0 0.45rem;
        }
        .kone-lede {
            max-width: 790px;
            color: #526A80;
            font-size: 1.03rem;
            line-height: 1.65;
            margin: -0.3rem 0 1.5rem;
        }
        .kone-status {
            display: inline-block;
            font-size: .82rem;
            font-weight: 650;
            border-radius: 7px;
            padding: .25rem .6rem;
            border: 1px solid #DDE7EF;
            background: #EDF3F9;
            color: #244B6E;
        }
        .kone-status-approved, .kone-status-completed {
            color: #176148; background: #E9F6EF; border-color: #C4E8D5;
        }
        .kone-status-pending, .kone-status-in-progress {
            color: #7D5400; background: #FFF6E4; border-color: #F3DEAE;
        }
        .kone-status-rejected { color: #982F35; background: #FFF0F1; border-color: #F2CED1; }
        .kone-status-changes-requested, .kone-status-further-verification-required {
            color: #814A19; background: #FFF1E6; border-color: #F2D5BD;
        }
        .kone-panel {
            background: #FFFFFF;
            border: 1px solid #DDE7EF;
            border-radius: 12px;
            padding: 1rem 1.2rem;
            margin: .75rem 0;
        }
        .kone-future {
            display: inline-block;
            font-size: .74rem;
            color: #415B74;
            border: 1px solid #C7D7E7;
            background: #EAF1F8;
            border-radius: 6px;
            padding: .2rem .52rem;
            font-weight: 700;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def heading(category, title, description=""):
    st.markdown(
        f'<p class="kone-eyebrow">{escape(category)}</p>',
        unsafe_allow_html=True,
    )
    st.title(title)
    if description:
        st.markdown(
            f'<p class="kone-lede">{escape(description)}</p>',
            unsafe_allow_html=True,
        )


def status_tag(status):
    """Accessible text-based status tag for data from the database."""
    normalized = str(status or "Unknown").strip().lower().replace("_", "-").replace(" ", "-")
    allowed = {
        "approved", "completed", "pending", "in-progress", "rejected",
        "changes-requested", "further-verification-required",
    }
    cls = normalized if normalized in allowed else "neutral"
    readable = str(status or "Unknown").replace("_", " ").title()
    st.markdown(
        f'<span class="kone-status kone-status-{cls}">{escape(readable)}</span>',
        unsafe_allow_html=True,
    )


def prototype_notice():
    st.caption(
        "Prototype access only. Role selection does not verify identity or "
        "restrict database access. Do not use with sensitive production data."
    )


def future_notice():
    st.markdown('<span class="kone-future">FUTURE CONCEPT • NOT IMPLEMENTED</span>', unsafe_allow_html=True)
