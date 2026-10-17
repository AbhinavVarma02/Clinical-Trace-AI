"""Deterministic input and output guards for the clinical agent.

These rules run outside any prompt, so they still apply when the language
model ignores its instructions. They classify a question before tools run,
flag instruction-like text inside retrieved records (records are data, never
instructions), and check generated text for unsafe clinical assertions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.llm_explainer import BANNED_OUTPUT_PHRASES, UNSAFE_OUTPUT_PATTERNS

MAX_QUESTION_CHARS = 1000

INJECTION_PATTERNS = [
    re.compile(r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instruction|instructions|rules|prompt|guardrail)", re.I),
    re.compile(r"\b(system|developer)\s+(prompt|note|message|mode)\b", re.I),
    re.compile(r"\byou are now\b|\bact as (an?|the)\b|\bjailbreak\b|\bDAN\b", re.I),
    re.compile(r"\b(reveal|print|show|output|dump)\b.{0,40}\b(prompt|instructions|database|every patient|all patients)",
               re.I),
]
EXFILTRATION_PATTERNS = [
    re.compile(r"\b(all|every|other|another|each)\s+(synthetic\s+)?patients?\b", re.I),
    re.compile(r"\blist\s+(the\s+)?patients\b|\bwhole database\b|\bentire database\b", re.I),
]
SENSITIVE_IDENTIFIER_PATTERNS = [
    re.compile(r"\b(ssn|social security|home address|phone number|email address|credit card|insurance number)\b", re.I),
]
CLINICAL_DECISION_PATTERNS = [
    re.compile(r"\b(should|can|could|must)\s+(i|we|they|you|he|she|the (patient|team|clinician))\b.{0,60}"
               r"\b(start|stop|increase|decrease|change|adjust|prescribe|give|take|switch|discontinue|"
               r"resume|hold|treat|admit|discharge)\b", re.I),
    # Dose questions are decisions only when they ask what *should* be given; asking what
    # dose is documented in the record is an ordinary record question.
    re.compile(r"\b(what|which|how much)\b.{0,40}\b(dose|dosage|insulin|warfarin|medication|drug)\b.{0,40}"
               r"\b(should|to give|to take|to start|would you|do you recommend|is best|is right|is safe)\b", re.I),
    re.compile(r"\bprescrib\w*\b", re.I),
    re.compile(r"\b(recommend|suggest|advise)\w*\b.{0,40}\b(treatment|medication|dose|therapy|drug|regimen)\b", re.I),
    re.compile(r"\b(diagnose|make a diagnosis|what is (the|his|her|their) diagnosis)\b", re.I),
    re.compile(r"\bwhat\s+(treatment|therapy)\b.{0,30}\b(should|best|needed)\b", re.I),
]
CERTAINTY_PATTERNS = [
    re.compile(r"\bwill\s+(he|she|they|the patient)\s+(definitely\s+)?(die|survive|be readmitted|recover)\b", re.I),
    re.compile(r"\bguarantee\b|\b100\s?%\s+(sure|certain)\b", re.I),
]


@dataclass(frozen=True)
class GuardResult:
    verdict: str  # "allow" | "refuse"
    category: str
    message: str
    matched: tuple[str, ...] = field(default_factory=tuple)

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"


def classify_question(
    question: str,
    patient_id: str,
    other_patient_markers: tuple[str, ...] = (),
) -> GuardResult:
    """Classify a user question before any tool runs."""
    text = question.strip()
    if not text:
        return GuardResult("refuse", "empty", "Please enter a question about the selected synthetic patient.")
    if len(text) > MAX_QUESTION_CHARS:
        return GuardResult("refuse", "too_long", f"Questions are limited to {MAX_QUESTION_CHARS} characters.")
    lowered = text.lower()
    for marker in other_patient_markers:
        if marker and marker.lower() != patient_id.lower() and re.search(rf"\b{re.escape(marker.lower())}\b", lowered):
            return GuardResult(
                "refuse", "cross_patient",
                "This session is scoped to one synthetic patient. Questions about other patients are not answered here.",
                (marker,),
            )
    for _name, patterns, category, message in (
        ("injection", INJECTION_PATTERNS, "prompt_injection",
         "The request asks the assistant to override its rules, so it was not processed."),
        ("exfiltration", EXFILTRATION_PATTERNS, "data_exfiltration",
         "Only the selected synthetic patient's record can be queried in this session."),
        ("identifier", SENSITIVE_IDENTIFIER_PATTERNS, "sensitive_identifier",
         "Direct identifiers are not stored in these synthetic records and are never returned."),
        ("decision", CLINICAL_DECISION_PATTERNS, "clinical_decision",
         "This prototype does not diagnose, prescribe, or recommend treatment. It can summarise what the "
         "synthetic record documents, for example current medication orders or recent lab trends."),
        ("certainty", CERTAINTY_PATTERNS, "outcome_certainty",
         "Outcomes cannot be predicted with certainty. Where it applies, the readmission model gives a "
         "probability estimate only."),
    ):
        hits = tuple(p.pattern for p in patterns if p.search(text))
        if hits:
            return GuardResult("refuse", category, message, hits[:3])
    return GuardResult("allow", "in_scope", "Question is within the record-review scope.")


def flag_content(text: str) -> list[str]:
    """Flag retrieved record text that contains instruction-like content."""
    flags = []
    if any(p.search(text) for p in INJECTION_PATTERNS):
        flags.append("possible_instruction_injection")
    return flags


def check_output(text: str) -> list[str]:
    """Return unsafe-output violations in generated (not quoted) text."""
    lowered = text.lower()
    violations = [f"banned phrase: {phrase}" for phrase in BANNED_OUTPUT_PHRASES if phrase in lowered]
    violations += [f"unsafe pattern: {p.pattern[:40]}" for p in UNSAFE_OUTPUT_PATTERNS if p.search(text)]
    return violations
