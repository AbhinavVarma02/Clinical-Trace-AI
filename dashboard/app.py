"""Streamlit product dashboard for Clinical-Trace AI.

This module is presentation-only. All model behaviour, prediction outputs,
explanation logic, and metadata come unchanged from ``src`` — the dashboard
merely renders them in a premium, tabbed, healthcare-focused UI.
"""

from __future__ import annotations

import html
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from src import config
from src.fallback_explainer import generate_explanation as generate_fallback_explanation
from src.llm_explainer import generate_explanation as generate_llm_explanation
from src.predict import get_model_info, is_model_loaded, predict as run_prediction


SAMPLE_PATIENTS: dict[str, dict[str, Any]] = {
    "Synthetic baseline encounter": {
        "patient_id": "synthetic_001",
        "age": "[60-70)",
        "time_in_hospital": 4,
        "num_lab_procedures": 38,
        "num_procedures": 1,
        "num_medications": 12,
        "number_outpatient": 0,
        "number_emergency": 0,
        "number_inpatient": 0,
        "number_diagnoses": 6,
        "insulin": "No",
        "change": "No",
        "diabetesMed": "Yes",
    },
    "Synthetic high-utilization encounter": {
        "patient_id": "synthetic_014",
        "age": "[70-80)",
        "time_in_hospital": 8,
        "num_lab_procedures": 62,
        "num_procedures": 2,
        "num_medications": 24,
        "number_outpatient": 2,
        "number_emergency": 1,
        "number_inpatient": 3,
        "number_diagnoses": 9,
        "insulin": "Up",
        "change": "Ch",
        "diabetesMed": "Yes",
    },
    "Synthetic complex medication encounter": {
        "patient_id": "synthetic_027",
        "age": "[80-90)",
        "time_in_hospital": 6,
        "num_lab_procedures": 51,
        "num_procedures": 0,
        "num_medications": 29,
        "number_outpatient": 1,
        "number_emergency": 0,
        "number_inpatient": 2,
        "number_diagnoses": 8,
        "insulin": "Steady",
        "change": "Ch",
        "diabetesMed": "Yes",
    },
}

FIELD_DEFAULTS = SAMPLE_PATIENTS["Synthetic high-utilization encounter"]
HISTORY_DEFAULTS: dict[str, Any] = {
    "history_available": False,
    "prior_encounter_count": 0,
    "prior_inpatient_count": 0,
    "prior_emergency_count": 0,
    "prior_readmission_count": 0,
    "running_mean_time_in_hospital": 0.0,
}
AGE_OPTIONS = [
    "[0-10)",
    "[10-20)",
    "[20-30)",
    "[30-40)",
    "[40-50)",
    "[50-60)",
    "[60-70)",
    "[70-80)",
    "[80-90)",
    "[90-100)",
]
INSULIN_OPTIONS = ["No", "Steady", "Up", "Down"]
CHANGE_OPTIONS = ["No", "Ch"]
DIABETES_MED_OPTIONS = ["No", "Yes"]

# Accessibility: risk tiers are distinguished by text + glyph, never colour alone.
RISK_DISPLAY = {
    "High": {"tone": "red", "glyph": "▲", "sub": "At or above the decision threshold"},
    "Moderate": {"tone": "amber", "glyph": "◆", "sub": "Approaching the decision threshold"},
    "Low": {"tone": "green", "glyph": "▼", "sub": "Below the decision threshold"},
}

# Inline brand mark (pulse-in-shield) — self-contained, no external asset needed.
BRAND_MARK = (
    '<svg width="34" height="34" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
    '<path d="M24 3l16 5v12c0 11-6.8 18.3-16 22-9.2-3.7-16-11-16-22V8l16-5z" fill="rgba(255,255,255,0.14)" stroke="rgba(255,255,255,0.9)" stroke-width="1.6"/>'
    '<path d="M11 25h6l3-8 5 15 3-9 2 2h6" stroke="#5eead4" stroke-width="2.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
    "</svg>"
)


def inject_styles() -> None:
    """Apply a premium healthcare product skin (frontend only)."""
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap');
        :root {
            color-scheme: light;
            --navy: #0b2545;
            --navy-2: #14355f;
            --blue: #1c5d99;
            --teal: #0e7c86;
            --teal-soft: #5eead4;
            --bg: #eaf1f8;
            --card: #ffffff;
            --card-2: #f6fafd;
            --line: #dbe4ef;
            --line-soft: #e9eff7;
            --ink: #0f2033;
            --muted: #51637a;
            --muted-2: #6b7c93;
            --red: #b42318;
            --amber: #b54708;
            --green: #087443;
            --shadow-sm: 0 1px 2px rgba(15, 32, 51, 0.06), 0 1px 3px rgba(15, 32, 51, 0.05);
            --shadow: 0 8px 24px rgba(15, 32, 51, 0.08);
            --shadow-lg: 0 22px 55px rgba(11, 37, 69, 0.18);
            --radius: 16px;
        }
        html, body, .stApp, .stApp * {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
        }
        .stApp {
            background:
                radial-gradient(1100px 520px at 6% -12%, rgba(20, 90, 150, 0.12), transparent 60%),
                radial-gradient(950px 480px at 104% -6%, rgba(13, 148, 136, 0.10), transparent 55%),
                radial-gradient(800px 600px at 50% 120%, rgba(28, 93, 153, 0.08), transparent 60%),
                linear-gradient(180deg, #eef4fb 0%, #e8eff8 55%, #eaf2f7 100%) !important;
            color: var(--ink) !important;
        }
        .stApp h2, .stApp h3, .stApp h4,
        .stApp p, .stApp li, .stApp label, .stApp span,
        div[data-testid="stMarkdownContainer"],
        div[data-testid="stWidgetLabel"] p {
            color: var(--ink);
        }
        .block-container {
            padding-top: 1.5rem;
            padding-bottom: 2.8rem;
            max-width: 1300px;
        }
        /* Inputs */
        .stTextInput input, .stNumberInput input, textarea {
            background: #ffffff !important;
            color: var(--ink) !important;
            -webkit-text-fill-color: var(--ink) !important;
            border: 1px solid #c4d1e0 !important;
            border-radius: 10px !important;
        }
        .stTextInput input:focus, .stNumberInput input:focus {
            border-color: var(--blue) !important;
            box-shadow: 0 0 0 3px rgba(28, 93, 153, 0.14) !important;
        }
        div[data-baseweb="select"] > div {
            background: #ffffff !important;
            border-color: #c4d1e0 !important;
            border-radius: 10px !important;
            color: var(--ink) !important;
        }
        div[data-baseweb="select"] span, div[data-baseweb="select"] input {
            color: var(--ink) !important;
            -webkit-text-fill-color: var(--ink) !important;
        }
        div[data-testid="stWidgetLabel"] p {
            font-size: 0.82rem !important;
            font-weight: 600 !important;
            color: var(--muted) !important;
        }
        /* Buttons */
        .stButton > button, .stFormSubmitButton > button {
            background: var(--navy) !important;
            border: 1px solid var(--navy) !important;
            border-radius: 11px !important;
            color: #ffffff !important;
            font-weight: 700 !important;
            padding: 0.55rem 1rem !important;
            box-shadow: var(--shadow-sm) !important;
            transition: background .15s ease, box-shadow .15s ease, filter .15s ease !important;
        }
        .stButton > button *, .stFormSubmitButton > button * {
            color: #ffffff !important;
            -webkit-text-fill-color: #ffffff !important;
        }
        .stButton > button:hover {
            background: linear-gradient(180deg, var(--navy-2), var(--navy)) !important;
            box-shadow: var(--shadow) !important;
        }
        .stFormSubmitButton > button {
            background: linear-gradient(180deg, #1c5d99, #144e83) !important;
            border-color: #144e83 !important;
        }
        .stFormSubmitButton > button:hover { filter: brightness(1.06); }
        div[data-testid="stForm"] {
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: var(--radius);
            box-shadow: var(--shadow);
            padding: 1.15rem 1.2rem 0.7rem 1.2rem;
        }
        /* ---------- HERO ---------- */
        .hero-card {
            position: relative;
            background: linear-gradient(122deg, #071b35 0%, #0f335e 46%, #0e5a63 100%);
            border: 1px solid rgba(255, 255, 255, 0.14);
            border-radius: 20px;
            box-shadow: var(--shadow-lg);
            padding: 1.6rem 1.8rem;
            margin-bottom: 1.15rem;
            overflow: hidden;
        }
        .hero-card::before {
            content: ""; position: absolute; right: -70px; top: -90px;
            width: 300px; height: 300px; border-radius: 50%;
            background: radial-gradient(circle, rgba(94, 234, 212, 0.22), transparent 66%);
        }
        .hero-card::after {
            content: ""; position: absolute; left: -60px; bottom: -120px;
            width: 320px; height: 320px; border-radius: 50%;
            background: radial-gradient(circle, rgba(56, 132, 214, 0.20), transparent 68%);
        }
        .hero-row { position: relative; z-index: 1; display: flex; align-items: center; gap: 0.85rem; }
        .hero-mark {
            width: 52px; height: 52px; border-radius: 14px; flex: none;
            display: flex; align-items: center; justify-content: center;
            background: rgba(255, 255, 255, 0.08); border: 1px solid rgba(255, 255, 255, 0.18);
        }
        .hero-eyebrow {
            color: #7ff0dc !important; -webkit-text-fill-color: #7ff0dc !important;
            font-size: 0.72rem; font-weight: 800; letter-spacing: 0.16em;
            text-transform: uppercase; margin: 0 0 0.28rem 0;
        }
        .hero-title {
            color: #f8fafc !important; -webkit-text-fill-color: #f8fafc !important;
            font-size: 2.15rem; font-weight: 900; line-height: 1.05; margin: 0;
            text-shadow: 0 1px 18px rgba(0, 0, 0, 0.28);
        }
        .hero-subtitle {
            color: #dbeafe !important; -webkit-text-fill-color: #dbeafe !important;
            font-size: 1.05rem; font-weight: 600; margin: 0.4rem 0 0 0;
        }
        .hero-desc {
            position: relative; z-index: 1;
            color: #cfe0f4 !important; -webkit-text-fill-color: #cfe0f4 !important;
            font-size: 0.92rem; font-weight: 500; margin: 0.75rem 0 0 0; max-width: 780px; line-height: 1.5;
        }
        .hero-badges { position: relative; z-index: 1; display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 1.05rem; }
        .hero-badge {
            display: inline-flex; align-items: center; gap: 0.4rem;
            background: rgba(255, 255, 255, 0.09); border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 999px; padding: 0.32rem 0.72rem;
            font-size: 0.8rem; font-weight: 700;
            color: #eef5fd !important; -webkit-text-fill-color: #eef5fd !important;
        }
        .hero-badge .hb-dot { width: 8px; height: 8px; border-radius: 50%; background: #9fb6d0; flex: none; }
        .hero-badge.ok .hb-dot { background: #34d399; box-shadow: 0 0 0 3px rgba(52, 211, 153, 0.22); }
        .hero-badge.warn .hb-dot { background: #fbbf24; box-shadow: 0 0 0 3px rgba(251, 191, 36, 0.22); }
        .hero-badge.info .hb-dot { background: #60a5fa; box-shadow: 0 0 0 3px rgba(96, 165, 250, 0.22); }
        .hero-badge.off .hb-dot { background: #94a3b8; }
        /* ---------- SECTIONS ---------- */
        .section-head { margin: 0.4rem 0 0.7rem 0; }
        .section-eyebrow {
            font-size: 0.72rem; font-weight: 800; letter-spacing: 0.1em; text-transform: uppercase;
            color: var(--teal) !important; margin: 0 0 0.15rem 0;
        }
        .section-title { font-size: 1.22rem; font-weight: 800; color: var(--navy) !important; margin: 0; }
        .section-sub { font-size: 0.88rem; color: var(--muted) !important; margin: 0.25rem 0 0 0; }
        .soft-divider { height: 1px; background: linear-gradient(90deg, transparent, var(--line), transparent); border: 0; margin: 1.6rem 0 1.2rem 0; }
        .section-card {
            background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
            box-shadow: var(--shadow); padding: 1.1rem 1.2rem; margin-bottom: 0.5rem;
        }
        .card-title {
            display: flex; align-items: center; gap: 0.5rem;
            font-size: 0.72rem; font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase;
            color: var(--muted) !important; margin-bottom: 0.7rem;
        }
        .card-title::before {
            content: ""; width: 7px; height: 7px; border-radius: 50%;
            background: var(--teal); box-shadow: 0 0 0 3px rgba(14, 124, 134, 0.15);
        }
        /* Pills / badges */
        .pill {
            display: inline-flex; align-items: center; gap: 0.32rem;
            border-radius: 999px; padding: 0.24rem 0.62rem; font-size: 0.8rem; font-weight: 700;
            border: 1px solid var(--line); background: #eef2f7; color: var(--ink) !important;
        }
        .pill::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; opacity: 0.85; }
        .pill-green { background: #e5f6ec; color: var(--green) !important; border-color: #a7e3bf; }
        .pill-amber { background: #fdf1e3; color: var(--amber) !important; border-color: #f4cf9c; }
        .pill-red   { background: #fdeceb; color: var(--red) !important;   border-color: #f4b8b2; }
        .pill-blue  { background: #e7f0fa; color: #164e82 !important;       border-color: #b3d1ee; }
        .pill-gray  { background: #eef2f7; color: var(--muted) !important;  border-color: var(--line); }
        /* Metric grid */
        .metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 0.8rem; }
        .metric-card {
            background: linear-gradient(180deg, #ffffff, var(--card-2));
            border: 1px solid var(--line); border-radius: 14px;
            box-shadow: var(--shadow-sm); padding: 0.9rem 0.95rem; border-top: 3px solid var(--blue);
        }
        .metric-card.accent { border-top-color: var(--teal); }
        .metric-key { font-size: 0.72rem; font-weight: 700; color: var(--muted) !important; text-transform: uppercase; letter-spacing: 0.04em; }
        .metric-val { font-size: 1.72rem; font-weight: 800; color: var(--navy) !important; line-height: 1.15; margin-top: 0.15rem; }
        .metric-cap { font-size: 0.72rem; color: var(--muted-2) !important; margin-top: 0.12rem; }
        /* Risk block */
        .risk-badge {
            display: flex; align-items: center; gap: 0.75rem;
            border-radius: 14px; padding: 0.9rem 1rem; margin-bottom: 0.8rem; border: 1px solid var(--line);
        }
        .risk-badge.red   { background: linear-gradient(180deg, #fdeceb, #fbe2e0); border-color: #f2b6b0; }
        .risk-badge.amber { background: linear-gradient(180deg, #fdf3e6, #fbecd8); border-color: #f2cea0; }
        .risk-badge.green { background: linear-gradient(180deg, #e8f7ee, #ddf2e6); border-color: #a9e2bf; }
        .risk-glyph { font-size: 1.55rem; line-height: 1; }
        .risk-badge.red .risk-glyph, .risk-badge.red .risk-word { color: var(--red) !important; }
        .risk-badge.amber .risk-glyph, .risk-badge.amber .risk-word { color: var(--amber) !important; }
        .risk-badge.green .risk-glyph, .risk-badge.green .risk-word { color: var(--green) !important; }
        .risk-word { font-size: 1.3rem; font-weight: 800; }
        .risk-sub { font-size: 0.82rem; color: var(--muted) !important; }
        /* Probability bar */
        .prob-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 0.35rem; }
        .prob-num { font-size: 1.2rem; font-weight: 800; color: var(--navy) !important; }
        .prob-thr { font-size: 0.8rem; color: var(--muted) !important; }
        .prob-track { position: relative; height: 12px; border-radius: 999px; background: #e6edf5; overflow: hidden; border: 1px solid var(--line-soft); }
        .prob-fill { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 999px; background: linear-gradient(90deg, #1c5d99, #0e7c86); }
        .prob-marker { position: absolute; top: -3px; bottom: -3px; width: 2px; background: var(--navy); }
        .prob-legend { display: flex; align-items: center; gap: 0.35rem; margin-top: 0.4rem; font-size: 0.75rem; color: var(--muted) !important; }
        .prob-legend .dot { width: 9px; height: 2px; background: var(--navy); display: inline-block; }
        /* Feature bars */
        .feat-row { margin-bottom: 0.6rem; }
        .feat-top { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 0.22rem; gap: 0.5rem; }
        .feat-name { font-size: 0.88rem; font-weight: 600; color: var(--ink) !important; }
        .feat-dir { font-size: 0.72rem; font-weight: 700; white-space: nowrap; }
        .feat-dir.up { color: #164e82 !important; }
        .feat-dir.down { color: var(--teal) !important; }
        .feat-track { height: 9px; border-radius: 999px; background: #eef2f7; overflow: hidden; }
        .feat-fill { height: 100%; border-radius: 999px; }
        .feat-fill.up { background: linear-gradient(90deg, #2a6fb0, #164e82); }
        .feat-fill.down { background: linear-gradient(90deg, #14a89b, #0e7c86); }
        /* Governance rows */
        .gov-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem 1.5rem; }
        .gov-row { display: flex; justify-content: space-between; align-items: center; gap: 0.6rem; padding: 0.44rem 0; border-bottom: 1px dashed var(--line-soft); }
        .gov-key { font-size: 0.82rem; color: var(--muted) !important; font-weight: 600; }
        .gov-val { font-size: 0.86rem; color: var(--ink) !important; font-weight: 700; text-align: right; }
        /* Notes & lists */
        .note {
            font-size: 0.86rem; color: var(--muted) !important; line-height: 1.5;
            border-left: 3px solid var(--teal); background: var(--card-2);
            padding: 0.6rem 0.8rem; border-radius: 0 10px 10px 0; margin: 0.35rem 0;
        }
        .safety-list { list-style: none; padding: 0; margin: 0; }
        .safety-list li {
            display: flex; align-items: flex-start; gap: 0.55rem;
            padding: 0.44rem 0; font-size: 0.9rem; color: var(--ink) !important; border-bottom: 1px solid var(--line-soft);
        }
        .safety-list li:last-child { border-bottom: 0; }
        .safety-check {
            flex: none; width: 20px; height: 20px; border-radius: 50%;
            background: #e5f6ec; color: var(--green) !important;
            display: inline-flex; align-items: center; justify-content: center;
            font-size: 0.72rem; font-weight: 900; margin-top: 0.05rem;
        }
        .muted-inline { color: var(--muted) !important; font-size: 0.88rem; }
        div[data-testid="stDataFrame"] { border-radius: 12px; overflow: hidden; border: 1px solid var(--line); }
        /* ---------- TABS (a keyed radio styled as a premium segmented nav) ---------- */
        div[data-testid="stRadio"] > div[role="radiogroup"] {
            display: flex; flex-wrap: wrap; gap: 0.3rem;
            background: linear-gradient(180deg, #ffffff, var(--card-2));
            border: 1px solid var(--line); border-radius: 16px;
            padding: 0.4rem; box-shadow: var(--shadow-sm);
            margin: 0.1rem 0 1.4rem 0;
        }
        div[data-testid="stRadio"] > div[role="radiogroup"] > label {
            flex: 1 1 auto; display: flex; align-items: center; justify-content: center;
            margin: 0; padding: 0.6rem 1rem; border-radius: 11px;
            border: 1px solid transparent; cursor: pointer; background: transparent;
            transition: background .15s ease, box-shadow .15s ease;
        }
        /* Hide the native radio dot; the pill itself communicates selection. */
        div[data-testid="stRadio"] > div[role="radiogroup"] > label > div:first-child { display: none !important; }
        div[data-testid="stRadio"] > div[role="radiogroup"] > label p,
        div[data-testid="stRadio"] > div[role="radiogroup"] > label div {
            font-size: 0.92rem !important; font-weight: 700 !important; margin: 0;
            color: var(--muted) !important; white-space: nowrap; letter-spacing: 0.01em;
        }
        div[data-testid="stRadio"] > div[role="radiogroup"] > label:hover { background: #eaf1fa; }
        div[data-testid="stRadio"] > div[role="radiogroup"] > label:hover p,
        div[data-testid="stRadio"] > div[role="radiogroup"] > label:hover div { color: var(--navy) !important; }
        div[data-testid="stRadio"] > div[role="radiogroup"] > label:has(input:checked) {
            background: linear-gradient(180deg, var(--navy-2), var(--navy));
            border-color: var(--navy); box-shadow: 0 6px 16px rgba(11, 37, 69, 0.22);
        }
        div[data-testid="stRadio"] > div[role="radiogroup"] > label:has(input:checked) p,
        div[data-testid="stRadio"] > div[role="radiogroup"] > label:has(input:checked) div {
            color: #ffffff !important; -webkit-text-fill-color: #ffffff !important;
        }
        /* ---------- Cleaner chrome for screenshot-ready output ---------- */
        [data-testid="stToolbar"], [data-testid="stDecoration"], [data-testid="stStatusWidget"] { display: none !important; }
        header[data-testid="stHeader"] { background: transparent !important; }
        #MainMenu, footer { visibility: hidden; height: 0; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def safe_text(value: Any) -> str:
    """Escape values before placing them in HTML snippets."""
    return html.escape(str(value))


_WHITESPACE_RUN = re.compile(r"\s*\n\s*")


def write_html(markup: str) -> None:
    """Render HTML flattened to a single line.

    Streamlit's markdown renderer treats indented / blank-separated lines as
    code blocks, which breaks multi-element HTML (e.g. card grids). Collapsing
    inter-line whitespace to a single space keeps the markup as one raw-HTML
    block so it renders as intended.
    """
    st.markdown(_WHITESPACE_RUN.sub(" ", markup).strip(), unsafe_allow_html=True)


def metric_or_dash(metrics: dict[str, Any], key: str) -> str:
    """Format a metric from metadata if it is available."""
    value = metrics.get(key)
    if value is None:
        return "-"
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "-"


def section_head(eyebrow: str, title: str, sub: str | None = None) -> None:
    """Render a consistent section header."""
    sub_html = f'<p class="section-sub">{safe_text(sub)}</p>' if sub else ""
    write_html(
        f'<div class="section-head"><p class="section-eyebrow">{safe_text(eyebrow)}</p>'
        f'<p class="section-title">{safe_text(title)}</p>{sub_html}</div>'
    )


def initialize_form_state() -> None:
    """Seed Streamlit session state with synthetic sample values."""
    for key, value in FIELD_DEFAULTS.items():
        st.session_state.setdefault(key, value)
    for key, value in HISTORY_DEFAULTS.items():
        st.session_state.setdefault(key, value)
    st.session_state.setdefault("recent_predictions", [])
    st.session_state.setdefault("last_result", None)


def apply_sample(sample_name: str) -> None:
    """Load a synthetic patient example into form fields."""
    for key, value in SAMPLE_PATIENTS[sample_name].items():
        st.session_state[key] = value
    for key, value in HISTORY_DEFAULTS.items():
        st.session_state[key] = value


def current_payload() -> dict[str, Any]:
    """Build the simulator payload from Streamlit session state."""
    history_available = bool(st.session_state.history_available)
    return {
        "patient_id": str(st.session_state.patient_id),
        "age": str(st.session_state.age),
        "time_in_hospital": int(st.session_state.time_in_hospital),
        "num_lab_procedures": int(st.session_state.num_lab_procedures),
        "num_procedures": int(st.session_state.num_procedures),
        "num_medications": int(st.session_state.num_medications),
        "number_outpatient": int(st.session_state.number_outpatient),
        "number_emergency": int(st.session_state.number_emergency),
        "number_inpatient": int(st.session_state.number_inpatient),
        "number_diagnoses": int(st.session_state.number_diagnoses),
        "insulin": str(st.session_state.insulin),
        "change": str(st.session_state.change),
        "diabetesMed": str(st.session_state.diabetesMed),
        "prior_encounter_count": int(st.session_state.prior_encounter_count) if history_available else 0,
        "prior_inpatient_count": int(st.session_state.prior_inpatient_count) if history_available else 0,
        "prior_emergency_count": int(st.session_state.prior_emergency_count) if history_available else 0,
        "prior_readmission_count": int(st.session_state.prior_readmission_count) if history_available else 0,
        "running_mean_time_in_hospital": (
            float(st.session_state.running_mean_time_in_hospital) if history_available else 0.0
        ),
        "is_first_encounter": not history_available,
    }


def display_risk_label(probability: float, threshold: float) -> str:
    """Map model probability into a three-tier UI label without changing backend output."""
    if probability >= threshold:
        return "High"
    if probability >= max(0.15, threshold * 0.65):
        return "Moderate"
    return "Low"


def explanation_mode_label(mode: str) -> str:
    """Render explanation mode in product language."""
    return "LLM" if mode == "llm" else "Rule-based fallback"


def generate_explanation(prediction: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Generate an LLM explanation when configured, otherwise use offline fallback."""
    if config.LLM_AVAILABLE:
        return generate_llm_explanation(
            patient_id=prediction["patient_id"],
            risk_label=prediction["readmission_risk"],
            risk_probability=prediction["risk_probability"],
            top_features=prediction["top_features"],
            request_id=request_id,
            model_version=prediction["model_version"],
        )
    return generate_fallback_explanation(
        patient_id=prediction["patient_id"],
        risk_label=prediction["readmission_risk"],
        risk_probability=prediction["risk_probability"],
        top_features=prediction["top_features"],
        request_id=request_id,
        model_version=prediction["model_version"],
    )


def run_simulation() -> None:
    """Execute prediction and explanation for the current synthetic payload."""
    request_id = str(uuid4())
    prediction = run_prediction(current_payload())
    explanation = generate_explanation(prediction, request_id)
    threshold = float(prediction["risk_threshold"])
    risk_label = display_risk_label(float(prediction["risk_probability"]), threshold)
    result = {
        "request_id": request_id,
        "prediction": prediction,
        "explanation": explanation,
        "display_risk_label": risk_label,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    st.session_state.last_result = result
    st.session_state.recent_predictions.insert(
        0,
        {
            "timestamp": result["timestamp"],
            "patient_id": prediction["patient_id"],
            "risk_label": risk_label,
            "probability": f"{float(prediction['risk_probability']):.1%}",
            "explanation_mode": explanation_mode_label(explanation["explanation_mode"]),
        },
    )
    st.session_state.recent_predictions = st.session_state.recent_predictions[:10]


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #
def _hero_badge(label: str, value: str, tone: str) -> str:
    return (
        f'<span class="hero-badge {tone}"><span class="hb-dot"></span>'
        f'{safe_text(label)}: {safe_text(value)}</span>'
    )


def render_hero(model_metadata: dict[str, Any], model_loaded: bool) -> None:
    """Render the premium hero with guaranteed high-contrast white text."""
    threshold = float(model_metadata.get("risk_threshold", config.RISK_THRESHOLD))
    mode = "LLM" if config.LLM_AVAILABLE else "Rule-based fallback"
    badges = "".join(
        [
            _hero_badge("Model", "Loaded" if model_loaded else "Unavailable", "ok" if model_loaded else "off"),
            _hero_badge("Inference API", "Ready" if model_loaded else "Down", "ok" if model_loaded else "off"),
            _hero_badge("Explanation", mode, "info" if config.LLM_AVAILABLE else "warn"),
            _hero_badge("LangSmith", "Active" if config.LANGSMITH_AVAILABLE else "Offline", "ok" if config.LANGSMITH_AVAILABLE else "off"),
            _hero_badge("Threshold", f"{threshold:.2f}", "info"),
        ]
    )
    write_html(
        f"""
        <div class="hero-card">
            <div class="hero-row">
                <div class="hero-mark">{BRAND_MARK}</div>
                <div>
                    <p class="hero-eyebrow">Clinical decision-support &middot; AI observability</p>
                    <h1 class="hero-title" style="color:#f8fafc;">Clinical-Trace AI</h1>
                    <p class="hero-subtitle" style="color:#dbeafe;">Healthcare Readmission Risk &nbsp;+&nbsp; MLOps / LLMOps Observability</p>
                </div>
            </div>
            <p class="hero-desc">AI-assisted readmission risk scoring with explainability, model monitoring, and safety-aware LLM explanations.</p>
            <div class="hero-badges">{badges}</div>
            <div class="hero-badges" style="margin-top:0.6rem;">
                <span class="hero-badge"><span class="hb-dot" style="background:#7ff0dc;"></span>&#9873; {safe_text(config.SAFETY_DISCLAIMER)}</span>
            </div>
        </div>
        """
    )


def render_model_performance(model_metadata: dict[str, Any]) -> None:
    """Render deployed model metrics as premium cards (always visible, no tabs)."""
    metrics = dict(model_metadata.get("metrics", {}))
    model_name = str(model_metadata.get("model_name", "-"))
    model_type = str(model_metadata.get("model_type", "model"))
    threshold = float(model_metadata.get("risk_threshold", config.RISK_THRESHOLD))
    section_head(
        "Model performance",
        "Held-out test metrics",
        f"Deployed model: {model_type} ({model_name}) · operating threshold {threshold:.2f}",
    )
    cards = [
        ("Accuracy", "accuracy", "Overall correct rate", False),
        ("Balanced acc.", "balanced_accuracy", "Class-balanced accuracy", False),
        ("Recall", "recall_positive", "Readmissions caught", False),
        ("Precision", "precision_positive", "Alarm correctness", False),
        ("Specificity", "specificity", "Non-readmits cleared", False),
        ("F1", "f1_positive", "Precision / recall balance", False),
        ("F2", "f2_positive", "Recall-weighted balance", False),
        ("ROC-AUC", "roc_auc", "Overall ranking quality", True),
        ("PR-AUC", "pr_auc", "Rare-event ranking", True),
    ]
    card_html = []
    for label, key, caption, accent in cards:
        accent_class = " accent" if accent else ""
        card_html.append(
            f'<div class="metric-card{accent_class}">'
            f'<div class="metric-key">{safe_text(label)}</div>'
            f'<div class="metric-val">{metric_or_dash(metrics, key)}</div>'
            f'<div class="metric-cap">{safe_text(caption)}</div></div>'
        )
    write_html(f'<div class="metric-grid">{"".join(card_html)}</div>')
    write_html(
        '<div class="note" style="margin-top:1rem;">Metrics are reported on the untouched held-out test set. '
        'The operating threshold was selected on validation data (maximizing F1 with recall &ge; 0.65). '
        'ROC-AUC and PR-AUC (teal) are the ranking-quality signals; 30-day readmission is a rare event '
        '(~11% positive rate), so precision, F1, and PR-AUC are inherently bounded.</div>'
    )


def render_simulator() -> bool:
    """Render patient risk simulator controls and return whether to run."""
    write_html(
        '<div class="muted-inline">Choose a synthetic scenario or edit encounter values, then run the '
        'simulator. All identifiers are synthetic — no PHI.</div>'
    )
    st.write("")
    sample_cols = st.columns([2, 1])
    sample_name = sample_cols[0].selectbox("Synthetic example", list(SAMPLE_PATIENTS), label_visibility="collapsed")
    if sample_cols[1].button("Load sample patient", width="stretch"):
        apply_sample(sample_name)
        st.rerun()

    with st.form("patient_risk_simulator"):
        row1 = st.columns(3)
        row1[0].text_input("Synthetic patient ID", key="patient_id")
        row1[1].selectbox("Age bracket", AGE_OPTIONS, key="age")
        row1[2].number_input("Time in hospital", min_value=0, step=1, key="time_in_hospital")

        row2 = st.columns(3)
        row2[0].number_input("Diagnoses", min_value=0, step=1, key="number_diagnoses")
        row2[1].number_input("Lab procedures", min_value=0, step=1, key="num_lab_procedures")
        row2[2].number_input("Procedures", min_value=0, step=1, key="num_procedures")

        row3 = st.columns(3)
        row3[0].number_input("Medications", min_value=0, step=1, key="num_medications")
        row3[1].number_input("Inpatient visits", min_value=0, step=1, key="number_inpatient")
        row3[2].number_input("Outpatient visits", min_value=0, step=1, key="number_outpatient")

        row4 = st.columns(3)
        row4[0].number_input("Emergency visits", min_value=0, step=1, key="number_emergency")
        row4[1].selectbox("Insulin", INSULIN_OPTIONS, key="insulin")
        row4[2].selectbox("Medication change", CHANGE_OPTIONS, key="change")

        row5 = st.columns(3)
        row5[0].selectbox("Diabetes medication", DIABETES_MED_OPTIONS, key="diabetesMed")

        with st.expander("Prior history (optional)"):
            history_available = st.checkbox(
                "Prior encounter history is available",
                key="history_available",
            )
            history_row1 = st.columns(3)
            history_row1[0].number_input(
                "Prior encounters",
                min_value=0,
                step=1,
                key="prior_encounter_count",
                disabled=not history_available,
            )
            history_row1[1].number_input(
                "Prior inpatient visits",
                min_value=0,
                step=1,
                key="prior_inpatient_count",
                disabled=not history_available,
            )
            history_row1[2].number_input(
                "Prior emergency visits",
                min_value=0,
                step=1,
                key="prior_emergency_count",
                disabled=not history_available,
            )
            history_row2 = st.columns(2)
            history_row2[0].number_input(
                "Prior 30-day readmissions",
                min_value=0,
                step=1,
                key="prior_readmission_count",
                disabled=not history_available,
            )
            history_row2[1].number_input(
                "Mean prior hospital stay",
                min_value=0.0,
                step=0.5,
                format="%.1f",
                key="running_mean_time_in_hospital",
                disabled=not history_available,
            )

        return st.form_submit_button("Run risk simulation", width="stretch")


def render_prediction_result(result: dict[str, Any] | None) -> None:
    """Render the prediction result card with an accessible risk badge and probability bar."""
    if result is None:
        write_html(
            """
            <div class="section-card">
                <div class="card-title">Awaiting simulation</div>
                <div class="muted-inline">Load a synthetic example or enter synthetic encounter values, then run the
                simulator to generate a readmission-risk estimate.</div>
            </div>
            """
        )
        return

    prediction = result["prediction"]
    risk_label = result["display_risk_label"]
    conf = RISK_DISPLAY.get(risk_label, RISK_DISPLAY["Low"])
    tone = conf["tone"]
    probability = float(prediction["risk_probability"])
    threshold = float(prediction["risk_threshold"])
    prob_pct = max(0.0, min(100.0, probability * 100))
    thr_pct = max(0.0, min(100.0, threshold * 100))

    write_html(
        f"""
        <div class="section-card">
            <div class="card-title">Readmission risk estimate</div>
            <div class="risk-badge {tone}">
                <span class="risk-glyph">{conf['glyph']}</span>
                <div>
                    <div class="risk-word">{safe_text(risk_label)} risk</div>
                    <div class="risk-sub">{conf['sub']}</div>
                </div>
            </div>
            <div class="prob-head">
                <span class="prob-num">{probability:.1%}</span>
                <span class="prob-thr">Decision threshold {threshold:.2f}</span>
            </div>
            <div class="prob-track">
                <div class="prob-fill" style="width:{prob_pct:.1f}%"></div>
                <div class="prob-marker" style="left:{thr_pct:.1f}%"></div>
            </div>
            <div class="prob-legend"><span class="dot"></span> Decision threshold marker</div>
            <div style="display:flex;flex-wrap:wrap;gap:0.4rem;margin-top:0.85rem;">
                <span class="pill pill-gray">Model {safe_text(prediction['model_version'])}</span>
                <span class="pill pill-gray">Request {safe_text(result['request_id'])[:12]}</span>
            </div>
        </div>
        """
    )


def render_feature_contributions(result: dict[str, Any] | None) -> None:
    """Render model-contributing factors as premium horizontal bars."""
    write_html(
        '<div class="note">Model-ranked signals behind the score (SHAP-style contributions), not clinical '
        'causes. For review context only — not a diagnosis or treatment recommendation.</div>'
    )
    if result is None:
        write_html('<div class="muted-inline">Run a prediction to see top model-contributing factors.</div>')
        return

    feature_df = pd.DataFrame(result["prediction"]["top_features"])
    if feature_df.empty:
        write_html('<div class="muted-inline">No feature contributions were returned for this prediction.</div>')
        return

    feature_df["absolute_contribution"] = feature_df["contribution"].abs()
    feature_df["direction"] = feature_df["contribution"].map(lambda value: "Raises risk" if value >= 0 else "Lowers risk")
    feature_df = feature_df.sort_values("absolute_contribution", ascending=False)
    max_magnitude = float(feature_df["absolute_contribution"].max()) or 1.0

    rows_html = []
    for _, row in feature_df.iterrows():
        raises = row["contribution"] >= 0
        width = max(4.0, (float(row["absolute_contribution"]) / max_magnitude) * 100)
        dir_class = "up" if raises else "down"
        dir_glyph = "▲" if raises else "▼"
        rows_html.append(
            f'<div class="feat-row"><div class="feat-top">'
            f'<span class="feat-name">{safe_text(row["feature"])}</span>'
            f'<span class="feat-dir {dir_class}">{dir_glyph} {safe_text(row["direction"])} &middot; {float(row["contribution"]):+.3f}</span>'
            f'</div><div class="feat-track"><div class="feat-fill {dir_class}" style="width:{width:.1f}%"></div></div></div>'
        )
    write_html(f'<div class="section-card">{"".join(rows_html)}</div>')

    display_df = feature_df.rename(
        columns={
            "feature": "Feature",
            "contribution": "Contribution",
            "absolute_contribution": "Magnitude",
            "direction": "Direction",
        }
    )
    with st.expander("View contribution values"):
        st.dataframe(
            display_df[["Feature", "Direction", "Contribution", "Magnitude"]],
            width="stretch",
            hide_index=True,
        )


def render_explanation(result: dict[str, Any] | None) -> None:
    """Render the LLM or fallback explanation in a premium card."""
    if result is None:
        write_html('<div class="muted-inline">Run a prediction to generate an explanation.</div>')
        return

    explanation = result["explanation"]
    mode = explanation.get("explanation_mode", "rule-based")
    mode_label = explanation_mode_label(mode)
    mode_tone = "blue" if mode == "llm" else "amber"
    review_areas = explanation.get("suggested_review_areas") or []
    disclaimer = explanation.get("safety_disclaimer", config.SAFETY_DISCLAIMER)

    review_html = ""
    if review_areas:
        chips = "".join(f'<span class="pill pill-gray">{safe_text(area)}</span>' for area in review_areas)
        review_html = (
            '<div class="metric-key" style="margin-top:0.9rem;">Suggested review areas</div>'
            f'<div style="display:flex;flex-wrap:wrap;gap:0.4rem;margin-top:0.25rem;">{chips}</div>'
        )

    write_html(
        f"""
        <div class="section-card">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:0.5rem;margin-bottom:0.6rem;">
                <span class="card-title" style="margin:0;">Decision-support explanation</span>
                <span class="pill pill-{mode_tone}">{safe_text(mode_label)}</span>
            </div>
            <div style="font-size:0.95rem;line-height:1.6;color:var(--ink);">{safe_text(explanation["explanation"])}</div>
            {review_html}
            <div class="note" style="margin-top:0.95rem;">&#9873; {safe_text(disclaimer)}</div>
        </div>
        """
    )


def _gov_row(key: str, value: str) -> str:
    return f'<div class="gov-row"><span class="gov-key">{safe_text(key)}</span><span class="gov-val">{safe_text(value)}</span></div>'


def render_observability(model_metadata: dict[str, Any], model_loaded: bool) -> None:
    """Render an AI governance / observability panel."""
    threshold = float(model_metadata.get("risk_threshold", config.RISK_THRESHOLD))
    splits = model_metadata.get("split_patient_counts", {}) or {}
    split_text = "-"
    if splits:
        split_text = " / ".join(str(splits.get(part, "-")) for part in ("train", "validation", "test")) + " patients"

    gov_rows = [
        _gov_row("Selected model", str(model_metadata.get("model_name", "-"))),
        _gov_row("Estimator", str(model_metadata.get("model_type", "-"))),
        _gov_row("Model version", str(model_metadata.get("model_version", "local"))),
        _gov_row("Training date", str(model_metadata.get("training_date", "-"))),
        _gov_row("Decision threshold", f"{threshold:.2f}"),
        _gov_row("Selection metric", str(model_metadata.get("selection_metric", "-"))),
        _gov_row("Feature count", str(model_metadata.get("feature_count", "-"))),
        _gov_row("Split (train/val/test)", split_text),
    ]
    protocol = str(model_metadata.get("evaluation_protocol", "")).strip()
    dataset_hash = str(model_metadata.get("dataset_hash", ""))
    if dataset_hash:
        gov_rows.append(_gov_row("Dataset fingerprint", dataset_hash[:12] + "…"))

    status_chips = "".join(
        [
            f'<span class="pill {"pill-green" if model_loaded else "pill-red"}">Model {"loaded" if model_loaded else "unavailable"}</span>',
            f'<span class="pill {"pill-green" if config.LANGSMITH_AVAILABLE else "pill-gray"}">LangSmith {"active" if config.LANGSMITH_AVAILABLE else "offline"}</span>',
            f'<span class="pill {"pill-blue" if config.LLM_AVAILABLE else "pill-amber"}">{"LLM explanations" if config.LLM_AVAILABLE else "Offline fallback"}</span>',
            '<span class="pill pill-gray">MLflow tracked</span>',
        ]
    )
    protocol_html = f'<div class="note" style="margin-top:0.9rem;">{safe_text(protocol)}</div>' if protocol else ""

    write_html(
        f"""
        <div class="section-card governance-card">
            <div class="card-title">AI governance snapshot</div>
            <div style="display:flex;flex-wrap:wrap;gap:0.4rem;margin-bottom:0.85rem;">{status_chips}</div>
            <div class="gov-grid">{"".join(gov_rows)}</div>
            {protocol_html}
        </div>
        """
    )


def render_safety_panel() -> None:
    """Render safety and privacy posture."""
    offline_mode_note = (
        "Optional offline demo mode runs without OpenAI or LangSmith keys."
        if config.LLM_AVAILABLE
        else "Currently running in offline demo mode with rule-based explanations."
    )
    items = [
        "No PHI — synthetic patient identifiers only.",
        "Raw patient records are never sent to LLM providers.",
        "Decision-support only — not a diagnosis or a treatment recommendation.",
        "Guardrails reject unsafe or diagnostic language, with a rule-based fallback.",
        offline_mode_note,
    ]
    list_html = "".join(f'<li><span class="safety-check">&#10003;</span><span>{safe_text(item)}</span></li>' for item in items)
    write_html(
        f'<div class="section-card safety-card"><div class="card-title">Trust &amp; safety posture</div>'
        f'<ul class="safety-list">{list_html}</ul></div>'
    )


def render_recent_predictions() -> None:
    """Render recent predictions from session state."""
    if not st.session_state.recent_predictions:
        write_html('<div class="muted-inline">No predictions run yet in this session.</div>')
        return
    frame = pd.DataFrame(st.session_state.recent_predictions).rename(
        columns={
            "timestamp": "Time",
            "patient_id": "Synthetic ID",
            "risk_label": "Risk",
            "probability": "Probability",
            "explanation_mode": "Explanation",
        }
    )
    st.dataframe(frame, width="stretch", hide_index=True)


NAV_SECTIONS = [
    "Overview",
    "Model Performance",
    "Explainability",
    "Observability",
    "Trust & Safety",
    "Activity",
]


def render_simulator_panel() -> None:
    """Risk-simulator panel — scenario selector, inputs, and prediction card."""
    section_head("Risk simulator", "Patient scenario & prediction")
    sim_left, sim_right = st.columns([1.35, 0.85])
    with sim_left:
        try:
            submitted = render_simulator()
            if submitted:
                run_simulation()
                st.success("Prediction generated for synthetic encounter.")
        except Exception as exc:
            st.error(str(exc))
    with sim_right:
        render_prediction_result(st.session_state.last_result)


def render_overview(model_metadata: dict[str, Any], model_loaded: bool) -> None:
    """Overview tab — hero, status badges, and the risk-simulator panel.

    The hero already carries the status badges and the decision-support
    disclaimer; the simulator panel follows directly beneath it.
    """
    render_hero(model_metadata, model_loaded)
    render_simulator_panel()


def render_explainability_tab() -> None:
    """Explainability tab — contributing factors + decision-support narrative."""
    factors_left, factors_right = st.columns([1.05, 0.95])
    with factors_left:
        section_head("Explainability", "Model-contributing factors")
        render_feature_contributions(st.session_state.last_result)
    with factors_right:
        section_head("Narrative", "Decision-support explanation")
        render_explanation(st.session_state.last_result)


def main() -> None:
    """Run the tabbed Streamlit dashboard.

    Layout is organised into six clean tabs — Overview (hero + risk simulator),
    Model Performance, Explainability, Observability, Trust & Safety, and
    Activity — while every prediction, explanation, and metric value is rendered
    unchanged from ``src``.

    Navigation uses a keyed ``st.radio`` styled as a premium segmented control.
    Unlike ``st.tabs``, a keyed widget keeps the active section across the reruns
    triggered by form submission and "Load sample", so the user is never bounced
    back to the first tab after generating a prediction.
    """
    st.set_page_config(page_title="Clinical-Trace AI", page_icon="\U0001FA7A", layout="wide")
    inject_styles()
    initialize_form_state()

    model_metadata = get_model_info()
    model_loaded = is_model_loaded()

    active = st.radio(
        "Dashboard sections",
        NAV_SECTIONS,
        horizontal=True,
        label_visibility="collapsed",
        key="active_section",
    )

    if active == "Overview":
        render_overview(model_metadata, model_loaded)
    elif active == "Model Performance":
        render_model_performance(model_metadata)
    elif active == "Explainability":
        render_explainability_tab()
    elif active == "Observability":
        section_head("Observability", "Governance & model tracking")
        render_observability(model_metadata, model_loaded)
    elif active == "Trust & Safety":
        section_head("Trust", "Safety & privacy")
        render_safety_panel()
    elif active == "Activity":
        section_head("Activity", "Recent predictions · this session")
        render_recent_predictions()


if __name__ == "__main__":
    main()
