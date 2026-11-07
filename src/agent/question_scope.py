"""Bounded names used to select patient-scoped evidence for focused questions."""

from __future__ import annotations

import re

MEDICATION_NAMES = re.compile(
    r"\b(insulin|metformin|warfarin|lisinopril|furosemide|tiotropium|albuterol|prednisone|"
    r"amlodipine|metoprolol|glargine|apixaban|torsemide)\b",
    re.I,
)
MEDICATION_CLASSES = re.compile(r"\b(?:anticoagulants?|anticoagulation|anticoagulated|blood(?:\s+|-)thinners?)\b", re.I)
ANTICOAGULANT_CANDIDATES = ("warfarin", "apixaban")
SENTENCE = re.compile(r"(?<=[.!?])\s+")

NON_USE_CLASS_CONTEXT = re.compile(
    r"\b(family|mother|father|parents?|sisters?|brothers?|siblings?|daughters?|sons?|children|child|"
    r"wife|husband|spouse|grandmother|grandfather|aunts?|uncles?|relatives?|"
    r"allerg(?:y|ies|ic)|hypersensitiv(?:ity|e)|intoleran(?:ce|t)|"
    r"denies|denied|declined|refused|discuss\w*|hypothetic\w*|potential\w*|possible|"
    r"plans?|planned|planning|would|could|might|should)\b|"
    r"\bmay\s+(start|prescribe|take|use|continue)\b|"
    r"\bno\s+(prescriptions?|orders?|current\s+use|longer\s+(taking|using))\b",
    re.I,
)
NOT_DOCUMENTED = re.compile(
    r"\b(not|never|if|unless|consider\w*|possibly|probably|perhaps|uncertain|unclear|planned|proposed)\b|n't\b|"
    r"\b(should|may|might|could|would|will|can|must|to)\s+(?:have\s+been|be)\b",
    re.I,
)


def named_drugs(question: str) -> list[str]:
    """Exact named medications only; generic intent and class terms are not identities."""
    return list(dict.fromkeys(match.lower() for match in MEDICATION_NAMES.findall(question)))


def mentions(text: str, drug: str) -> bool:
    return bool(re.search(rf"\b{re.escape(drug)}\b", text, re.I))


def class_drugs(question: str) -> tuple[str, ...]:
    """An approved, nonexhaustive selection vocabulary, never patient-use evidence."""
    if not MEDICATION_CLASSES.search(question):
        return ()
    named = named_drugs(question)
    return tuple(drug for drug in ANTICOAGULANT_CANDIDATES if not named or drug in named)


def documented(entry: str) -> bool:
    """Reject negated, conditional, proposed or modal descriptions of an event."""
    return not NOT_DOCUMENTED.search(entry)


def patient_class_sentence(sentence: str, title: str, drugs: tuple[str, ...]) -> bool:
    """Use the whole sentence and section context to avoid attributing non-use to the patient."""
    return (
        any(mentions(sentence, drug) for drug in drugs)
        and not NON_USE_CLASS_CONTEXT.search(title + " " + sentence)
        and documented(sentence)
        and "?" not in sentence
    )


DIAGNOSTIC_NAME = re.compile(
    r"\b(stress tests?|(?:basic |comprehensive )?metabolic panels?|diagnostic panels?|"
    r"chest x-rays?|echocardiograms?|electrocardiograms?|ecg|ekg)\b",
    re.I,
)
DIAGNOSTIC_REQUEST = re.compile(
    r"\b(show|results?|findings?|conclusions?|impressions?|reveal|say|indicate|normal|abnormal|positive|negative)\b",
    re.I,
)
DIAGNOSTIC_DATE = re.compile(r"\b(?:19|20)\d{2}(?:-\d{2}(?:-\d{2})?)?\b")
UNSUPPORTED_DIAGNOSTIC_TIME = re.compile(
    r"\b(latest|last|first|prior|previous|recent|earliest|before|after|between|yesterday|today|"
    r"january|february|march|april|may|june|july|august|september|october|november|december)\b",
    re.I,
)


def diagnostic_subjects(question: str) -> tuple[str, ...]:
    if not DIAGNOSTIC_REQUEST.search(question):
        return ()
    return tuple(dict.fromkeys(match.lower().removesuffix("s") for match in DIAGNOSTIC_NAME.findall(question)))


def diagnostic_report_matches(name: str, subject: str) -> bool:
    return name.strip().casefold() == subject


def diagnostic_note_result(sentence: str, title: str, subject: str) -> bool:
    """A named test and its stated result must occur together in one attributable sentence."""
    return (
        not NON_USE_CLASS_CONTEXT.search(title + " " + sentence)
        and not re.search(r"\b(if|unless|scheduled|ordered|pending|expected)\b|\?", sentence, re.I)
        and bool(
            re.search(
                rf"\b{re.escape(subject)}\s+(?:(?:showed|demonstrated|revealed|reported|did not show)\s+[^.!?\s]"
                r"|was (?:normal|abnormal|positive|negative)\b)",
                sentence,
                re.I,
            )
        )
    )
