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
from decimal import Decimal
from typing import Any

from src.retrieval.lexical import tokenize
from src.safety.guards import check_output, flag_content

NUMBER = re.compile(r"(?<![\w.])[+-]?\d+(?:\.\d+)?")
# Spelling aliases only: no conversion between units or clinical synonyms.
UNIT_ALIASES = {
    "%": "%", "percent": "%", "percentage": "%",
    "mg": "mg", "milligram": "mg", "milligrams": "mg",
    "mcg": "mcg", "microgram": "mcg", "micrograms": "mcg",
    "g": "g", "gram": "g", "grams": "g",
    "kg": "kg", "kilogram": "kg", "kilograms": "kg",
    "mg/dl": "mg/dl", "mmol/l": "mmol/l", "g/dl": "g/dl",
    "ml": "ml", "milliliter": "ml", "milliliters": "ml",
    "l": "l", "liter": "l", "liters": "l", "mmhg": "mmhg",
}
UNIT = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(re.escape(u) for u in sorted(UNIT_ALIASES, key=len, reverse=True))
    + r")(?![\w])", re.I,
)
MEASUREMENT = re.compile(
    r"(?<![\w.])(?P<number>[+-]?\d+(?:\.\d+)?)\s*(?P<unit>(?:"
    + "|".join(re.escape(u) for u in sorted(set(UNIT_ALIASES.values()), key=len, reverse=True))
    + r")(?:/[a-z0-9]+(?:\.[a-z0-9]+)*)*)(?![\w/])", re.I,
)
UNIT_PER = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(u) for u in sorted(set(UNIT_ALIASES.values()), key=len, reverse=True))
    + r")\s+per\s+([a-z]+[0-9]*)\b", re.I,
)
MONTH_NAMES = (
    "january february march april may june july august september october november december".split()
)
MONTH_PATTERN = "(?:" + "|".join(m[:3] + "(?:" + m[3:] + ")?" for m in MONTH_NAMES) + ")"
DATE_MENTION = re.compile(
    r"(?<![\w-])(?:"
    r"(?P<iso_year>\d{4})-(?P<iso_month>\d{2})(?:-(?P<iso_day>\d{2}))?"
    rf"|(?P<md_month>{MONTH_PATTERN})\s+(?P<md_day>\d{{1,2}})(?:,)?\s+(?P<md_year>\d{{4}})"
    rf"|(?P<dm_day>\d{{1,2}})\s+(?P<dm_month>{MONTH_PATTERN})\s+(?P<dm_year>\d{{4}})"
    rf"|(?P<my_month>{MONTH_PATTERN})\s+(?P<my_year>\d{{4}})"
    r")(?![\w-])", re.I,
)
HISTORICAL = re.compile(
    r"^(?:(?:the\s+)?patient|he|she|they)\s+(?:was|were)\s+"
    r"(?P<verb>diagnosed\s+with|prescribed)\s+(?P<payload>.+?)\.?$", re.I,
)
INTERVAL_SOURCE = re.compile(
    r"^The inpatient admission on (?P<admission>\d{4}-\d{2}-\d{2}) began (?P<gap>\d+) days "
    r"after discharge on (?P<discharge>\d{4}-\d{2}-\d{2}); this interval (?P<window>[^.]+)\. "
    r"Sources: (?P<previous>Encounter/[^\s@]+)@v(?P<previous_version>\d+) \(discharge\) and "
    r"(?P<current>Encounter/[^\s@]+)@v(?P<current_version>\d+) \(admission\)\.$"
)
INTERVAL_CLAIMS = [re.compile(pattern, re.I) for pattern in (
    r"^(?:the\s+)?(?:inpatient\s+)?(?:admission|readmission)\s+on\s+@DATE0@\s+"
    r"(?:began|occurred|was)\s+(?P<gap>[+]?\d+(?:\.0+)?)\s+days?\s+after\s+"
    r"(?:the\s+)?discharge\s+on\s+@DATE1@(?P<tail>.*)$",
    r"^on\s+@DATE0@,?\s+(?:the\s+)?(?:inpatient\s+)?(?:admission|readmission)\s+"
    r"(?:began|occurred|was)\s+(?P<gap>[+]?\d+(?:\.0+)?)\s+days?\s+after\s+"
    r"(?:the\s+)?discharge\s+on\s+@DATE1@(?P<tail>.*)$",
)]
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
    # Dates have their own atomic check; their day/month components must not
    # accidentally substantiate an unrelated dose or result.
    return {_number(token) for token in NUMBER.findall(_without_dates(text))}


def _number(token: str) -> str:
    value = Decimal(token)
    if not value:
        return "0"
    # Decimal.normalize() applies the process precision and can round long
    # values into each other. Formatting preserves every supplied digit.
    exact = format(value, "f")
    return exact.rstrip("0").rstrip(".") if "." in exact else exact


def _dates(text: str) -> list[tuple[re.Match[str], tuple[int, int, int | None] | None]]:
    mentions = []
    for match in DATE_MENTION.finditer(text):
        prefix = next(p for p in ("iso", "md", "dm", "my") if match.group(f"{p}_year"))
        year = int(match.group(f"{prefix}_year"))
        raw_month = match.group(f"{prefix}_month")
        month = int(raw_month) if prefix == "iso" else next(
            i + 1 for i, name in enumerate(MONTH_NAMES) if name.startswith(raw_month.lower()[:3])
        )
        raw_day = match.group(f"{prefix}_day") if prefix != "my" else None
        day = int(raw_day) if raw_day else None
        try:
            date(year, month, day if day is not None else 1)
            value = (year, month, day)
        except ValueError:
            value = None
        mentions.append((match, value))
    return mentions


def _without_dates(text: str) -> str:
    return DATE_MENTION.sub(" ", text)


def _dates_supported(text: str, cited_text: str) -> bool:
    available = [value for _, value in _dates(cited_text) if value is not None]
    return all(
        value is not None and any(
            value == source or (value[2] is None and value[:2] == source[:2]) for source in available
        ) for _, value in _dates(text)
    )


def _units(text: str) -> str:
    canonical = UNIT.sub(lambda m: UNIT_ALIASES[m.group().lower()], text)
    while True:
        joined = UNIT_PER.sub(r"\1/\2", canonical)
        if joined == canonical:
            return joined
        canonical = joined


def _measurements(text: str) -> set[tuple[str, str]]:
    return {(_number(m.group("number")), m.group("unit").lower())
            for m in MEASUREMENT.finditer(_units(_without_dates(text)))}


def _check_interval_claim(text: str, cited: list[EvidenceItem]) -> list[str]:
    """Bind computed interval claims to ordered dates, their gap, and window.

    Numeric/date atom membership cannot distinguish a gap from the thirty-day
    threshold or reverse admission and discharge roles. Only bounded explicit
    relations are accepted for this trusted derived evidence kind.
    """
    intervals = [item for item in cited if item.kind == "encounter_interval"]
    if not intervals:
        return []
    prefix = "computed encounter interval failed: "
    if len(intervals) != 1:
        return [prefix + "claim must cite exactly one computed interval"]
    item = intervals[0]
    source = INTERVAL_SOURCE.fullmatch(item.text)
    if source is None:
        return [prefix + "unrecognised tool interval structure"]
    try:
        admission = date.fromisoformat(source["admission"])
        discharge = date.fromisoformat(source["discharge"])
        expected = (admission - discharge).days
        gap = int(source["gap"])
    except ValueError:
        return [prefix + "invalid tool interval dates or gap"]
    if expected < 0 or gap != expected:
        return [prefix + "tool gap does not match ordered event dates"]
    if (item.source_ref != source["current"] or str(item.source_version) != source["current_version"]):
        return [prefix + "tool admission source/version differs from its lineage"]
    window = ("meets the thirty-day readmission definition (0 to 30 days)" if gap <= 30
              else "is outside the thirty-day readmission window (0 to 30 days)")
    if source["window"] != window:
        return [prefix + "tool window classification does not match its gap"]
    mentions = _dates(text)
    if len(mentions) != 2 or any(value is None or value[2] is None for _, value in mentions):
        return [prefix + "claim needs two explicit full event dates"]
    canonical = text
    for index, (mention, _) in reversed(list(enumerate(mentions))):
        canonical = canonical[:mention.start()] + f"@DATE{index}@" + canonical[mention.end():]
    parsed = next((match for pattern in INTERVAL_CLAIMS if (match := pattern.fullmatch(canonical))), None)
    if parsed is None:
        return [prefix + "unsupported event relation wording"]
    claim_admission, claim_discharge = (value for _, value in mentions)
    if (claim_admission != (admission.year, admission.month, admission.day)
            or claim_discharge != (discharge.year, discharge.month, discharge.day)):
        return [prefix + "claim admission/discharge roles differ from the tool interval"]
    if _number(parsed["gap"]) != str(gap):
        return [prefix + "claim gap differs from the recomputed interval"]
    tail = " ".join(parsed["tail"].lower().split()).strip(" .;,")
    short_window = window.split(" (")[0]
    allowed = {""}
    for description in (window, short_window):
        allowed.update((description, f"this interval {description}", f"which {description}"))
    # An exact full tool quotation may include the source lineage as well.
    allowed.add(" ".join(item.text[source.end("discharge"):].lower().split()).strip(" .;,"))
    if tail not in allowed:
        return [prefix + "unsupported window classification or additional content"]
    return []


def _historical_record_text(text: str, cited: list[EvidenceItem]) -> str:
    """Remove a historical trigger only after exact, same-item record support.

    This exception is deliberately limited to a whole past-tense sentence and
    structured condition/medication evidence. Free-form notes and generic token
    overlap cannot establish a diagnosis or a prescription.
    """
    historical = HISTORICAL.fullmatch(text)
    if not historical:
        return text
    payload = historical.group("payload")
    mentions = _dates(payload)
    if mentions:
        # A supported historical statement may end in one explicit date.
        if len(mentions) != 1 or payload[mentions[0][0].end():].strip(" ."):
            return text
        before = payload[:mentions[0][0].start()]
        if not re.search(r"\s(?:on|in)\s+$", before, re.I):
            return text
        payload = re.sub(r"\s(?:on|in)\s+$", "", before, flags=re.I)
    words = " ".join(re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", _units(payload).lower()))
    if not words or check_output(payload) or flag_content(payload):
        return text
    kind = "condition" if historical.group("verb").lower().startswith("diagnosed") else "medication"
    for item in cited:
        if item.kind != kind or item.flags or flag_content(item.text):
            continue
        source = " ".join(re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", _units(_without_dates(item.text)).lower()))
        # A medication's update date is not its order date. The structured
        # event field, rather than any date elsewhere in the item, anchors history.
        event = re.search(r"\bonset\s+" if kind == "condition" else r"\bordered\s+", item.text, re.I)
        event_dates = _dates(item.text[event.end():]) if event else []
        event_date = event_dates[0][0].group() if event_dates and event_dates[0][0].start() == 0 else ""
        if (source.startswith(words + " ") and _dates_supported(text, event_date)
                and _measurements(payload) <= _measurements(item.text)):
            return payload
    return text


def lexical_support(text: str, cited_text: str) -> tuple[float, list[str]]:
    """Share of the claim's content tokens found in the cited evidence, plus the missing tokens."""
    claim_tokens = {
        t for t in tokenize(_units(_without_dates(text)))
        if len(t) > 1 and not NUMBER.fullmatch(t) and t not in TEMPLATE_WORDS
    }
    if not claim_tokens:
        return 1.0, []
    evidence_tokens = set(tokenize(_units(_without_dates(cited_text))))
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
            if item.flags or flag_content(item.text):
                reasons.append(f"citation '{citation}' contains flagged record content")
                continue
            cited.append(item)
        if cited:
            available = set().union(*(_numbers(item.text) for item in cited))
            cited_text = " ".join(item.text for item in cited)
            interval_reasons = _check_interval_claim(text, cited)
            reasons.extend(interval_reasons)
            if not _dates_supported(text, cited_text):
                reasons.append("dates not found in cited evidence")
            if not _measurements(text) <= _measurements(cited_text):
                reasons.append("number-unit pairs not found in cited evidence")
            for computation in claim.get("computations") or []:
                ok, detail = _check_computation(computation, cited_text, len(citations))
                if ok:
                    available |= _numbers(str(computation.get("result")))
                else:
                    reasons.append(f"computation failed: {detail}")
            missing = sorted(n for n in _numbers(text) if n not in available)
            if missing:
                reasons.append(f"numbers not found in cited evidence: {', '.join(missing)}")
            lexical_text = _historical_record_text(text, cited)
            if not interval_reasons:
                interval = next((item for item in cited if item.kind == "encounter_interval"), None)
                if interval is not None:
                    # The entire relation and optional qualifier were proved
                    # above. Canonicalize only this bounded grammar, so an
                    # equivalent 'occurred' is not an unsupported clinical word.
                    lexical_text = interval.text
            support, unsupported = lexical_support(lexical_text, cited_text)
            claim["lexical_support"] = round(support, 3)
            if support < MIN_LEXICAL_SUPPORT:
                reasons.append(f"low lexical support ({support:.2f}); unsupported terms: "
                               f"{', '.join(unsupported[:6])}")
        violations = check_output(_historical_record_text(text, cited))
        if flag_content(text):
            violations.append("unsafe instruction-like output")
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
