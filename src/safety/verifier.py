"""Evidence verification for agent answers.

Each claim must cite evidence collected *in the same run* for the *same
patient*, and every number stated in the claim must appear in the cited
evidence text. Claims that fail are removed and reported; an answer with no
surviving claims becomes an abstention.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from src.retrieval.lexical import tokenize
from src.safety.guards import check_output

NUMBER = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?")
# Minimum share of a claim's content words that must occur in its cited evidence.
# A lexical heuristic that catches off-topic or unsupported generations; it is not
# semantic entailment and is documented as such in docs/EVALUATION.md.
MIN_LEXICAL_SUPPORT = 0.6
# Connective words used by the deterministic templates; they carry no clinical content.
TEMPLATE_WORDS = frozenset(tokenize(
    "admission admissions meets recorded record conditions condition onset encounter encounters between inpatient stay began day days "
    "after discharge which meets thirty readmission definition active medication orders order history status "
    "note documentation gap possible inconsistency overall first latest result model outputs output "
    "probability threshold version label high low top contributions contribution shap raise lower score "
    "born male female unit no end time not because so the and"
    # Relation words a paraphrase may use without introducing clinical content.
    " increased increase decreased decrease rose risen fell fallen declined decline changed reduced reduction"
    " consistent inconsistent inconsistency across reported documented documents multiple another some both"
    " initially initial later then due during while whereas over from to were was value values level levels"
    " trend trended dose dosed patient history notes note mentioned indicates indicated states stated"
))


@dataclass
class EvidenceItem:
    evidence_id: str
    patient_id: str
    source_ref: str
    source_version: str
    text: str
    kind: str
    title: str = ""
    date: str = ""
    flags: list[str] = field(default_factory=list)
    scores: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "patient_id": self.patient_id,
            "source_ref": self.source_ref,
            "source_version": self.source_version,
            "text": self.text,
            "kind": self.kind,
            "title": self.title,
            "date": self.date,
            "flags": self.flags,
            "scores": self.scores,
        }


def _numbers(text: str) -> set[str]:
    normalised = set()
    for token in NUMBER.findall(text):
        normalised.add(token)
        if "." in token:
            normalised.add(token.rstrip("0").rstrip("."))
        else:
            normalised.add(token.lstrip("0") or "0")
    return normalised


def lexical_support(text: str, cited_text: str) -> tuple[float, list[str]]:
    """Share of the claim's content tokens found in the cited evidence, plus the missing tokens."""
    claim_tokens = {
        t for t in tokenize(text)
        if len(t) > 1 and not NUMBER.fullmatch(t) and t not in TEMPLATE_WORDS
    }
    if not claim_tokens:
        return 1.0, []
    evidence_tokens = set(tokenize(cited_text))
    missing = sorted(claim_tokens - evidence_tokens)
    return 1 - len(missing) / len(claim_tokens), missing


def _check_computation(computation: dict[str, Any], cited_text: str, citation_count: int) -> tuple[bool, str]:
    """Recompute a derived value; its inputs must appear in the cited evidence."""
    op = computation.get("op")
    args = [str(a) for a in computation.get("args") or []]
    result = str(computation.get("result"))
    try:
        if op == "days_between" and len(args) == 2:
            if not all(a in cited_text for a in args):
                return False, "dates not present in cited evidence"
            expected = abs((date.fromisoformat(args[1]) - date.fromisoformat(args[0])).days)
            return str(expected) == result, f"days_between={expected}"
        if op == "count":
            return str(citation_count) == result, f"count={citation_count}"
        if op == "difference" and len(args) == 2:
            if not all(re.search(rf"(?<![\d.]){re.escape(a)}(?![\d])", cited_text) for a in args):
                return False, "operands not present in cited evidence"
            expected = round(float(args[1]) - float(args[0]), 3)
            return abs(expected - float(result)) < 1e-9, f"difference={expected:g}"
    except ValueError as exc:
        return False, f"invalid computation ({exc})"
    return False, f"unsupported computation '{op}'"


def verify_claims(
    claims: list[dict[str, Any]],
    evidence: dict[str, EvidenceItem],
    patient_id: str,
) -> dict[str, Any]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    isolation_violations = 0
    for claim in claims:
        text = str(claim.get("text", "")).strip()
        citations = [str(c) for c in claim.get("citations") or []]
        reasons: list[str] = []
        if not text:
            reasons.append("empty claim")
        if not citations:
            reasons.append("no citation")
        cited = []
        for citation in citations:
            item = evidence.get(citation)
            if item is None:
                reasons.append(f"citation '{citation}' is not in this run's evidence")
                continue
            if item.patient_id != patient_id:
                isolation_violations += 1
                reasons.append(f"citation '{citation}' belongs to another patient")
                continue
            cited.append(item)
        if cited:
            available = set().union(*(_numbers(item.text) for item in cited))
            cited_text = " ".join(item.text for item in cited)
            for computation in claim.get("computations") or []:
                ok, detail = _check_computation(computation, cited_text, len(citations))
                if ok:
                    available |= _numbers(str(computation.get("result")))
                else:
                    reasons.append(f"computation failed: {detail}")
            missing = sorted(n for n in _numbers(text) if n not in available)
            if missing:
                reasons.append(f"numbers not found in cited evidence: {', '.join(missing)}")
            support, unsupported = lexical_support(text, cited_text)
            claim["lexical_support"] = round(support, 3)
            if support < MIN_LEXICAL_SUPPORT:
                reasons.append(f"low lexical support ({support:.2f}); unsupported terms: "
                               f"{', '.join(unsupported[:6])}")
        violations = check_output(text)
        reasons.extend(violations)
        record = {**claim, "text": text, "citations": citations}
        if reasons:
            rejected.append({**record, "reasons": reasons})
        else:
            accepted.append(record)
    total = len(claims)
    if not accepted:
        status = "failed"
    elif rejected:
        status = "partially_verified"
    else:
        status = "verified"
    return {
        "status": status,
        "accepted": accepted,
        "rejected": rejected,
        "claims_total": total,
        "claims_supported": len(accepted),
        "supported_ratio": round(len(accepted) / total, 4) if total else 0.0,
        "isolation_violations": isolation_violations,
    }
