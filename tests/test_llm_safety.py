"""LLM safety and fallback tests."""

from __future__ import annotations

import pytest

from src import config
from src.config import SAFETY_DISCLAIMER
from src.fallback_explainer import generate_explanation as generate_fallback_explanation
from src.llm_explainer import generate_explanation as generate_llm_explanation
from src.llm_explainer import load_prompt_template
import src.llm_explainer as llm_explainer


def test_prompt_template_loads_from_file():
    template = load_prompt_template("explanation_prompt_v2.txt")
    assert "STRICT RULES" in template
    assert "{patient_id}" in template


def test_fallback_contains_disclaimer_and_safe_language():
    output = generate_fallback_explanation(
        patient_id="synthetic_001",
        risk_label="high",
        risk_probability=0.42,
        top_features=[{"feature": "number inpatient", "contribution": 0.2}],
        request_id="test",
        model_version="test-version",
    )
    explanation = output["explanation"].lower()
    assert SAFETY_DISCLAIMER in output["explanation"]
    assert "diagnose" not in explanation
    assert "prescribe" not in explanation
    assert "i recommend you take" not in explanation
    assert "clinical literature" not in explanation
    assert "studies show" not in explanation
    assert "research indicates" not in explanation
    assert "medication adjustments" not in explanation
    assert "treatment adjustments" not in explanation
    assert "potential adjustments" not in explanation
    assert output["prompt_version"] == "rule-based"


def test_llm_failure_falls_back(monkeypatch):
    class Message:
        def __init__(self, content):
            self.content = content

    class FailingChat:
        def __init__(self, *args, **kwargs):
            pass

        def invoke(self, *args, **kwargs):
            raise RuntimeError("no external calls in tests")

    monkeypatch.setattr(config, "LLM_AVAILABLE", True)
    monkeypatch.setattr(llm_explainer, "ChatOpenAI", FailingChat)
    monkeypatch.setattr(llm_explainer, "HumanMessage", Message)
    monkeypatch.setattr(llm_explainer, "SystemMessage", Message)
    monkeypatch.setattr(llm_explainer, "RunnableConfig", lambda **kwargs: kwargs)

    output = generate_llm_explanation(
        patient_id="synthetic_001",
        risk_label="high",
        risk_probability=0.42,
        top_features=[{"feature": "number inpatient", "contribution": 0.2}],
        request_id="test",
        model_version="test-version",
    )
    assert output["explanation_mode"] == "rule-based"
    assert SAFETY_DISCLAIMER in output["explanation"]


def test_safe_llm_output_is_returned(monkeypatch):
    class Message:
        def __init__(self, content):
            self.content = content
            self.id = "safe-mock-trace"

    class SafeChat:
        def __init__(self, *args, **kwargs):
            pass

        def invoke(self, *args, **kwargs):
            return Message(
                "The model's analysis identifies inpatient visits as a model-ranked "
                f"factor for the care team to review. {SAFETY_DISCLAIMER}"
            )

    monkeypatch.setattr(config, "LLM_AVAILABLE", True)
    monkeypatch.setattr(llm_explainer, "ChatOpenAI", SafeChat)
    monkeypatch.setattr(llm_explainer, "HumanMessage", Message)
    monkeypatch.setattr(llm_explainer, "SystemMessage", Message)
    monkeypatch.setattr(llm_explainer, "RunnableConfig", lambda **kwargs: kwargs)

    output = generate_llm_explanation(
        patient_id="synthetic_001",
        risk_label="high",
        risk_probability=0.42,
        top_features=[{"feature": "Inpatient visits", "contribution": 0.2}],
        request_id="test",
        model_version="test-version",
    )
    assert output["explanation_mode"] == "llm"
    assert output["trace_id"] == "safe-mock-trace"


@pytest.mark.parametrize(
    "unsafe_content",
    [
        "The diagnosis is pneumonia.",
        "You should take aspirin.",
        "The patient needs surgery.",
        "The patient should start insulin.",
        "Increase the medication dose.",
        "I recommend treatment with surgery.",
    ],
)
def test_explicit_diagnostic_and_treatment_language_falls_back(monkeypatch, unsafe_content):
    class Message:
        def __init__(self, content):
            self.content = content

    class UnsafeChat:
        def __init__(self, *args, **kwargs):
            pass

        def invoke(self, *args, **kwargs):
            return Message(f"{unsafe_content} {SAFETY_DISCLAIMER}")

    monkeypatch.setattr(config, "LLM_AVAILABLE", True)
    monkeypatch.setattr(llm_explainer, "ChatOpenAI", UnsafeChat)
    monkeypatch.setattr(llm_explainer, "HumanMessage", Message)
    monkeypatch.setattr(llm_explainer, "SystemMessage", Message)
    monkeypatch.setattr(llm_explainer, "RunnableConfig", lambda **kwargs: kwargs)

    output = generate_llm_explanation(
        patient_id="synthetic_001",
        risk_label="high",
        risk_probability=0.42,
        top_features=[{"feature": "Inpatient visits", "contribution": 0.2}],
        request_id="test",
        model_version="test-version",
    )
    assert output["explanation_mode"] == "rule-based"
    assert unsafe_content.lower() not in output["explanation"].lower()
    assert SAFETY_DISCLAIMER in output["explanation"]


def test_llm_output_with_medication_adjustment_language_falls_back(monkeypatch):
    class Message:
        def __init__(self, content):
            self.content = content

    class UnsafeChat:
        def __init__(self, *args, **kwargs):
            pass

        def invoke(self, *args, **kwargs):
            return Message(
                "The model's analysis suggests evaluating the current medication "
                "regimen for potential adjustments. "
                f"{SAFETY_DISCLAIMER}"
            )

    monkeypatch.setattr(config, "LLM_AVAILABLE", True)
    monkeypatch.setattr(llm_explainer, "ChatOpenAI", UnsafeChat)
    monkeypatch.setattr(llm_explainer, "HumanMessage", Message)
    monkeypatch.setattr(llm_explainer, "SystemMessage", Message)
    monkeypatch.setattr(llm_explainer, "RunnableConfig", lambda **kwargs: kwargs)

    output = generate_llm_explanation(
        patient_id="synthetic_001",
        risk_label="high",
        risk_probability=0.42,
        top_features=[{"feature": "number inpatient", "contribution": 0.2}],
        request_id="test",
        model_version="test-version",
    )
    assert output["explanation_mode"] == "rule-based"
    assert SAFETY_DISCLAIMER in output["explanation"]
    assert "potential adjustments" not in output["explanation"].lower()


def test_explanation_is_safe_rejects_adjustment_language():
    unsafe_text = (
        "The model's analysis suggests treatment adjustments and medication "
        f"adjustments may be needed. {SAFETY_DISCLAIMER}"
    )
    assert llm_explainer.explanation_is_safe(unsafe_text) is False
