"""Optional LangChain LLM explanation layer with rule-based fallback.

When an OpenAI key is configured, formats a safety-constrained prompt, calls the
model, and validates the output before returning it; otherwise (or on any error
or failed safety check) it defers to ``src.fallback_explainer``. Invoked by the
``/explain`` route.

Safety: only synthetic IDs, risk scores, and top feature *names* are sent to the
provider — never raw patient records — and outputs are checked for the required
disclaimer and banned phrases before being returned.
"""

from __future__ import annotations

import os
import re
from typing import Any

from src import config
from src.config import SAFETY_DISCLAIMER
from src.fallback_explainer import generate_explanation as fallback_generate_explanation
from src.tracing import safe_trace_metadata

try:  # pragma: no cover - optional dependency import surface
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_core.runnables import RunnableConfig
    from langchain_openai import ChatOpenAI
except Exception:  # pragma: no cover
    HumanMessage = None
    SystemMessage = None
    RunnableConfig = None
    ChatOpenAI = None


PROMPT_VERSION = "explanation_prompt_v2.txt"
BANNED_OUTPUT_PHRASES = [
    "clinical literature",
    "studies show",
    "research indicates",
    "i recommend you take",
    "recommend medication",
    "change medication",
    "guarantee",
    "social determinant",
    "socioeconomic",
    "underlying health condition",
    "underlying medical condition",
    "underlying condition",
    "medication adjustments",
    "treatment adjustments",
    "potential adjustments",
]

# Fail-closed checks for unsafe clinical assertions or recommendations. These
# patterns intentionally target actions/assertions rather than standalone words
# such as "diagnosis", which may legitimately appear in a supplied model feature
# name (for example, "Number of diagnoses").
UNSAFE_OUTPUT_PATTERNS = [
    re.compile(
        r"\b(?:the|a|this|patient(?:'s)?)\s+diagnosis\s+"
        r"(?:is|was|appears|seems|indicates|suggests|confirms)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bdiagnos(?:e|ed|ing)\s+(?:the patient\s+)?(?:with|as)\b", re.IGNORECASE),
    re.compile(r"\bprescrib(?:e|es|ed|ing)\b|\bprescription\b", re.IGNORECASE),
    re.compile(
        r"\b(?:you|the patient|patient)\s+"
        r"(?:should|must|needs?\s+to|requires?\s+to)\s+"
        r"(?:take|start|stop|begin|discontinue|increase|decrease|change|adjust|receive|undergo)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:you|the patient|patient)\s+(?:needs?|requires?|should|must)\s+"
        r"(?:an?\s+)?(?:surgery|operation|treatment|therapy|procedure|medication|medicine|drug)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:recommend|recommends|recommended|suggest|suggests|suggested|advise|advises|advised)\b"
        r".{0,50}\b(?:medication|medicine|drug|dose|dosage|treatment|therapy|surgery|operation)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:start|stop|begin|discontinue|increase|decrease|raise|lower|adjust|change)\b"
        r".{0,35}\b(?:medication|medicine|drug|dose|dosage|insulin|aspirin)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:medication|medicine|drug|dose|dosage|insulin|aspirin)\b"
        r".{0,35}\b(?:start|stop|begin|discontinue|increase|decrease|raise|lower|adjust|change)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\btreat(?:ed|ing)?\s+(?:the patient\s+)?with\b", re.IGNORECASE),
]


def load_prompt_template(filename: str = PROMPT_VERSION) -> str:
    """Load a prompt template from the prompts directory."""
    path = config.PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8")


def format_top_features(top_features: list[Any]) -> str:
    """Format top features for the prompt without raw clinical values."""
    rows: list[str] = []
    for index, feature in enumerate(top_features[:5], start=1):
        if isinstance(feature, dict):
            name = feature.get("feature", "model feature")
        elif isinstance(feature, (list, tuple)) and feature:
            name = feature[0]
        else:
            name = getattr(feature, "feature", "model feature")
        rows.append(f"{index}. {name}")
    return "\n".join(rows) if rows else "No top features were available."


def explanation_is_safe(text: str) -> bool:
    """Validate the disclaimer and reject unsafe clinical language."""
    lower_text = text.lower()
    if SAFETY_DISCLAIMER not in text:
        return False
    if any(phrase in lower_text for phrase in BANNED_OUTPUT_PHRASES):
        return False
    return not any(pattern.search(text) for pattern in UNSAFE_OUTPUT_PATTERNS)


def _feature_names(top_features: list[Any]) -> list[str]:
    names = []
    for feature in top_features[:5]:
        if isinstance(feature, dict):
            names.append(str(feature.get("feature", "model feature")))
        elif isinstance(feature, (list, tuple)) and feature:
            names.append(str(feature[0]))
        else:
            names.append(str(getattr(feature, "feature", "model feature")))
    return names


def generate_explanation(
    patient_id: str,
    risk_label: str,
    risk_probability: float,
    top_features: list[Any],
    request_id: str,
    model_version: str,
) -> dict[str, Any]:
    """Generate an LLM explanation when configured, otherwise fall back safely."""
    if not config.LLM_AVAILABLE or ChatOpenAI is None or HumanMessage is None or SystemMessage is None:
        return fallback_generate_explanation(
            patient_id,
            risk_label,
            risk_probability,
            top_features,
            request_id=request_id,
            model_version=model_version,
        )

    try:
        safety_prompt = load_prompt_template("safety_prompt.txt")
        explanation_prompt = load_prompt_template(PROMPT_VERSION).format(
            patient_id=patient_id,
            risk_label=risk_label,
            risk_probability=risk_probability,
            model_version=model_version,
            top_features_formatted=format_top_features(top_features),
            rag_context="No retrieved sources were provided for this MVP.",
        )
        feature_names = _feature_names(top_features)
        metadata = safe_trace_metadata(
            request_id=request_id,
            patient_id=patient_id,
            model_version=model_version,
            risk_score=risk_probability,
            risk_label=risk_label,
            prompt_version=PROMPT_VERSION,
            top_feature_names=feature_names,
            disclaimer_present=True,
            no_treatment_recommendation=True,
        )
        llm = ChatOpenAI(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=0.2,
            timeout=20,
        )
        runnable_config = RunnableConfig(metadata=metadata) if RunnableConfig else None
        response = llm.invoke(
            [SystemMessage(content=safety_prompt), HumanMessage(content=explanation_prompt)],
            config=runnable_config,
        )
        content = str(getattr(response, "content", response)).strip()
        if not explanation_is_safe(content):
            raise ValueError("LLM explanation failed safety validation.")
        return {
            "request_id": request_id,
            "patient_id": patient_id,
            "risk_label": risk_label,
            "risk_probability": float(risk_probability),
            "explanation": content,
            "risk_drivers": feature_names,
            "suggested_review_areas": ["Top model-ranked factors", "Care coordination context"],
            "safety_disclaimer": SAFETY_DISCLAIMER,
            "prompt_version": PROMPT_VERSION,
            "explanation_mode": "llm",
            "trace_id": getattr(response, "id", None),
            "model_version": model_version,
        }
    except Exception:
        return fallback_generate_explanation(
            patient_id,
            risk_label,
            risk_probability,
            top_features,
            request_id=request_id,
            model_version=model_version,
        )
