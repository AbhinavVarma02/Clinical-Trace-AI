"""Tool planning: deterministic intent rules, or language-model tool selection.

The deterministic planner maps question intents to tools with explicit rules.
The LLM planner asks the configured model to choose tools from the allow-list;
its choice is validated (known tool, valid arguments, within budget) and falls
back to the deterministic plan if invalid. The plan source is always reported.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from src.agent.llm import OpenAICompatibleProvider, ProviderUnavailable, extract_json
from src.agent.tools import PLANNER_TOOLS, TOOLS

MAX_TOOL_CALLS = 8

LAB_TERMS = {
    "hba1c": "HbA1c", "a1c": "HbA1c", "egfr": "eGFR", "kidney": "eGFR", "inr": "INR", "glucose": "Glucose",
    "bnp": "BNP", "potassium": "Potassium", "hemoglobin": "Hemoglobin", "fev1": "FEV1",
    "oxygen": "Oxygen saturation", "saturation": "Oxygen saturation",
}

INTENT_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("risk", re.compile(r"\b(risk|predict\w*|shap|model|likelihood|probability)\b", re.I)),
    ("missing_information", re.compile(r"\b(missing|incomplete|gaps?|undocumented|completeness)\b", re.I)),
    ("inconsistency", re.compile(r"\b(contradict\w*|inconsisten\w*|conflict\w*|discrepan\w*|mismatch\w*|"
                                 r"disagree\w*)\b", re.I)),
    ("review_preparation", re.compile(r"\b(prepare|handoff|hand-off|review packet|for (human )?review)\b", re.I)),
    ("summary", re.compile(r"\b(summar\w*|overview|journey|course|tell me about|background)\b", re.I)),
    ("medications", re.compile(r"\b(medications?|meds?|drugs?|insulin|metformin|warfarin|lisinopril|furosemide|"
                               r"tiotropium|albuterol|prednisone|amlodipine|metoprolol|glargine|dose|dosage|"
                               r"orders?)\b", re.I)),
    ("labs", re.compile(r"\b(labs?|hba1c|a1c|egfr|inr|glucose|bnp|potassium|hemoglobin|fev1|oxygen|saturation|"
                        r"trend\w*|results?|kidney function)\b", re.I)),
    ("encounters", re.compile(r"\b(admission\w*|admitted|encounters?|visits?|readmi\w*|hospital\w*|stay|"
                              r"discharg\w*|emergency|follow-?up)\b", re.I)),
    ("conditions", re.compile(r"\b(conditions?|diagnos[ie]s|problem list|comorbid\w*|disease)\b", re.I)),
    ("smoking", re.compile(r"\b(smok\w*|tobacco|cigarettes?)\b", re.I)),
]

INTENT_TOOLS: dict[str, list[str]] = {
    "risk": ["get_readmission_prediction", "get_model_explanations"],
    "missing_information": ["detect_missing_information"],
    "inconsistency": ["detect_inconsistencies", "retrieve_clinical_notes"],
    "review_preparation": ["get_patient_profile", "get_patient_conditions", "get_patient_encounters",
                           "get_medication_history", "detect_missing_information", "detect_inconsistencies",
                           "get_readmission_prediction"],
    "summary": ["get_patient_profile", "get_patient_conditions", "get_patient_encounters",
                "get_medication_history", "get_lab_observations", "detect_missing_information"],
    "medications": ["get_medication_history", "retrieve_clinical_notes"],
    "labs": ["get_lab_observations", "retrieve_clinical_notes"],
    "encounters": ["get_patient_encounters", "retrieve_clinical_notes"],
    "conditions": ["get_patient_conditions"],
    "smoking": ["detect_inconsistencies", "retrieve_clinical_notes"],
    "record_qa": ["retrieve_clinical_notes"],
}


@dataclass
class Plan:
    intents: list[str]
    calls: list[dict[str, Any]]
    source: str  # "deterministic-rules" | "llm" | "deterministic-fallback"
    notes: list[str] = field(default_factory=list)
    latency_ms: float | None = None


def detect_intents(question: str) -> list[str]:
    intents = [name for name, pattern in INTENT_RULES if pattern.search(question)]
    return intents or ["record_qa"]


def lab_filter(question: str) -> str | None:
    lowered = question.lower()
    for term, test in LAB_TERMS.items():
        if re.search(rf"\b{term}\b", lowered):
            return test
    return None


def deterministic_plan(question: str) -> Plan:
    intents = detect_intents(question)
    calls: list[dict[str, Any]] = []
    seen: set[str] = set()
    for intent in intents:
        for tool in INTENT_TOOLS[intent]:
            if tool in seen:
                continue
            seen.add(tool)
            args: dict[str, Any] = {}
            if tool == "retrieve_clinical_notes":
                args = {"query": question[:300], "k": 5}
            elif tool == "get_lab_observations":
                args = {"test": lab_filter(question)} if intent == "labs" else {}
            calls.append({"name": tool, "args": args})
    return Plan(intents=intents, calls=calls[:MAX_TOOL_CALLS], source="deterministic-rules")


PLANNER_SYSTEM = (
    "You select tools for a read-only clinical record review assistant working on ONE synthetic patient. "
    "The patient is fixed by the session; never pass a patient id. Choose the smallest set of tools whose "
    "output is needed to answer the question from the record. You cannot diagnose or recommend treatment. "
    'Reply with JSON only: {"tool_calls": [{"name": "<tool>", "args": {...}}]} with at most 5 calls.'
)


YEAR = re.compile(r"\b(19|20)\d{2}\b")


def _sanitize_args(name: str, raw: Any, question: str) -> dict[str, Any]:
    """Keep only schema-defined arguments; never a patient id; dates only if the question names a year."""
    if not isinstance(raw, dict):
        return {}
    allowed = set(TOOLS[name].args_model.model_json_schema().get("properties", {}))
    args = {k: v for k, v in raw.items() if k in allowed and k != "patient_id" and v not in (None, "")}
    if not YEAR.search(question):
        args.pop("date_from", None)
        args.pop("date_to", None)
    if "k" in args:
        try:
            args["k"] = max(3, min(int(args["k"]), 10))
        except (TypeError, ValueError):
            args.pop("k")
    return args


def llm_plan(question: str, provider: OpenAICompatibleProvider) -> Plan:
    fallback = deterministic_plan(question)
    catalog = [
        {"name": name, "description": TOOLS[name].description,
         "args": {k: v.get("type", "string") for k, v in
                  TOOLS[name].args_model.model_json_schema().get("properties", {}).items()}}
        for name in PLANNER_TOOLS
    ]
    user = f"Tools:\n{json.dumps(catalog, indent=1)}\n\nQuestion: {question}"
    try:
        completion = provider.complete(PLANNER_SYSTEM, user, max_tokens=300)
        payload = extract_json(completion.text)
    except (ProviderUnavailable, ValueError) as exc:
        fallback.source = "deterministic-fallback"
        fallback.notes.append(f"LLM planner unavailable ({type(exc).__name__}); used rule-based plan.")
        return fallback
    calls = []
    notes = []
    for call in (payload.get("tool_calls") if isinstance(payload, dict) else None) or []:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name", ""))
        if name not in PLANNER_TOOLS:
            notes.append(f"rejected unknown tool '{name[:40]}'")
            continue
        args = _sanitize_args(name, call.get("args"), question)
        if name == "retrieve_clinical_notes" and not args.get("query"):
            args["query"] = question[:300]
        if any(c["name"] == name for c in calls):
            continue
        calls.append({"name": name, "args": args})
    if not calls:
        fallback.source = "deterministic-fallback"
        fallback.notes.extend(notes + ["LLM planner returned no valid tool calls; used rule-based plan."])
        return fallback
    return Plan(intents=fallback.intents, calls=calls[:MAX_TOOL_CALLS], source="llm", notes=notes,
                latency_ms=completion.latency_ms)
