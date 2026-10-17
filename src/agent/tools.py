"""Typed, authorised, patient-scoped tools available to the Clinical Case Review Agent.

Design rules enforced here (not in prompts):

* The patient id is bound by the session (``ToolContext.patient_id``); no tool
  accepts a patient id from the model, so a model cannot redirect a tool to
  another patient.
* Every call is authorised through the central policy before it runs.
* Every call runs under a timeout and returns a ``ToolResult``; exceptions are
  converted into structured errors.
* Tools return evidence items (with source reference and version) for every
  fact they expose; record *text* is never written to traces or logs.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.clinical.consistency import find_inconsistencies
from src.clinical.records import ClinicalRecords
from src.clinical.risk_bridge import assess
from src.retrieval.index import RetrievalIndex
from src.safety.policy import AccessContext, Action, AuthorizationError, PolicyEngine
from src.safety.verifier import EvidenceItem

_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="agent-tool")


@dataclass
class ToolContext:
    access: AccessContext
    policy: PolicyEngine
    records: ClinicalRecords
    index: RetrievalIndex
    patient_id: str
    predictor: Callable[[dict[str, Any]], dict[str, Any]] | None = None


@dataclass
class ToolResult:
    tool: str
    ok: bool
    data: Any = None
    evidence: list[EvidenceItem] = field(default_factory=list)
    error: str | None = None
    error_type: str | None = None
    latency_ms: float = 0.0

    def summary(self) -> dict[str, Any]:
        """Trace-safe summary: no record text."""
        return {
            "tool": self.tool,
            "ok": self.ok,
            "evidence_count": len(self.evidence),
            "error_type": self.error_type,
            "error": self.error if self.error_type != "internal" else "internal tool error",
            "latency_ms": round(self.latency_ms, 2),
        }


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NotesQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(..., min_length=2, max_length=300)
    k: int = Field(5, ge=1, le=10)
    date_from: str | None = None
    date_to: str | None = None


class LabQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test: str | None = Field(None, max_length=60, description="Optional test name filter, e.g. 'HbA1c'")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    action: Action
    handler: Callable[[ToolContext, BaseModel], tuple[Any, list[EvidenceItem]]]
    timeout_s: float = 5.0
    planner_visible: bool = True


# --------------------------------------------------------------- evidence
def _evidence(ctx: ToolContext, source: dict[str, Any], text: str, kind: str, title: str = "",
              date: str = "") -> EvidenceItem:
    return EvidenceItem(
        evidence_id=f"{source['reference'].split('/', 1)[1]}@v{source['version']}",
        patient_id=ctx.patient_id,
        source_ref=source["reference"],
        source_version=str(source["version"]),
        text=text,
        kind=kind,
        title=title,
        date=date or "",
    )


def _date(value: str | None) -> str:
    return (value or "")[:10]


# --------------------------------------------------------------- handlers
def _profile(ctx: ToolContext, _: BaseModel) -> tuple[Any, list[EvidenceItem]]:
    profile = ctx.records.profile(ctx.patient_id)
    if profile is None:
        raise LookupError("patient not found")
    text = (f"{profile['display_name']}: synthetic patient {profile['patient_id']}, {profile['gender']}, born {profile['birth_date']}, "
            f"age {profile['age_at_last_encounter']} at last encounter.")
    return profile, [_evidence(ctx, profile["source"], text, "profile", "Patient profile")]


def _encounters(ctx: ToolContext, _: BaseModel) -> tuple[Any, list[EvidenceItem]]:
    items = ctx.records.encounters(ctx.patient_id)
    evidence = []
    for enc in items:
        stay = f", ending {_date(enc['end'])}, length of stay {enc['length_of_stay_days']} days" \
            if enc["length_of_stay_days"] is not None else ", no end time recorded"
        text = f"{enc['class_display'].removesuffix(' encounter')} encounter starting {_date(enc['start'])} for {enc['reason']}{stay}."
        evidence.append(_evidence(ctx, enc["source"], text, "encounter", enc["reason"], enc["start"] or ""))
    return items, evidence


def _conditions(ctx: ToolContext, _: BaseModel) -> tuple[Any, list[EvidenceItem]]:
    items = ctx.records.conditions(ctx.patient_id)
    evidence = [
        _evidence(ctx, c["source"], f"{c['display']} (ICD-10 {c['code']}), {c['clinical_status']}, "
                                    f"onset {_date(c['onset'])}.", "condition", c["display"], c["onset"] or "")
        for c in items
    ]
    return items, evidence


def _medications(ctx: ToolContext, _: BaseModel) -> tuple[Any, list[EvidenceItem]]:
    items = ctx.records.medications(ctx.patient_id)
    evidence = []
    for med in items:
        for version in med["history"]:
            notes = f" Note: {' '.join(version['notes'])}" if version["notes"] else ""
            text = (f"{version['medication']}: {version['dosage']}; status {version['status']}; "
                    f"ordered {_date(version['authored_on'])}; record version {version['source']['version']} "
                    f"updated {_date(version['source']['last_updated'])}.{notes}")
            evidence.append(_evidence(ctx, version["source"], text, "medication", version["medication"],
                                      version["source"]["last_updated"] or ""))
    return items, evidence


def _labs(ctx: ToolContext, args: BaseModel) -> tuple[Any, list[EvidenceItem]]:
    assert isinstance(args, LabQuery)
    series = ctx.records.lab_trends(ctx.patient_id)
    if args.test:
        needle = args.test.lower().replace(" ", "")
        aliases = {"a1c": "hemoglobina1c", "hba1c": "hemoglobina1c"}
        needle = aliases.get(needle, needle)
        series = [s for s in series if needle in s["name"].lower().replace(" ", "")] or series
    evidence = []
    for entry in series:
        for point in entry["points"]:
            unit = point["unit"] or "(no unit recorded)"
            evidence.append(_evidence(ctx, point["source"], f"{entry['name']} {point['value']} {unit} on "
                                                            f"{_date(point['effective'])}.",
                                      "lab", entry["name"], point["effective"]))
    return series, evidence


def _timeline(ctx: ToolContext, _: BaseModel) -> tuple[Any, list[EvidenceItem]]:
    events = ctx.records.timeline(ctx.patient_id)
    compact = [{"when": e["when"], "kind": e["kind"], "label": e["label"], "source": e["source"]} for e in events]
    evidence = [_evidence(ctx, e["source"], f"{_date(e['when'])}: {e['label']}.", "timeline", e["kind"], e["when"])
                for e in events if e["kind"] in ("encounter", "medication", "document")]
    return compact, evidence


def _notes(ctx: ToolContext, args: BaseModel) -> tuple[Any, list[EvidenceItem]]:
    assert isinstance(args, NotesQuery)
    response = ctx.index.search(ctx.patient_id, args.query, k=args.k, date_from=args.date_from,
                                date_to=args.date_to)
    evidence = []
    for hit in response["results"]:
        item = EvidenceItem(
            evidence_id=hit["chunk_id"], patient_id=hit["patient_id"], source_ref=hit["source_ref"],
            source_version=hit["source_version"],
            text=f"{hit['title']} - {hit['section']} ({_date(hit['date'])}): {hit['text']}", kind="retrieved",
            title=f"{hit['title']} - {hit['section']}", date=hit["date"], flags=list(hit["flags"]),
            scores=hit["scores"],
        )
        evidence.append(item)
    return {"results": [{k: v for k, v in hit.items() if k != "text"} for hit in response["results"]],
            "diagnostics": response["diagnostics"]}, evidence


def _prediction(ctx: ToolContext, _: BaseModel) -> tuple[Any, list[EvidenceItem]]:
    assessment = assess(ctx.records, ctx.patient_id)
    if not assessment["available"] or ctx.predictor is None:
        reasons = assessment.get("reasons") or ["model artifacts are not loaded"]
        return {"available": False, "reasons": reasons}, []
    prediction = ctx.predictor(assessment["features"])
    data = {"available": True, "encounter_id": assessment["encounter_id"], "prediction": prediction,
            "features": assessment["features"], "caveats": assessment["caveats"]}
    source = next(s for s in assessment["sources"] if s["reference"].startswith("Encounter/"))
    text = (f"Readmission model output for encounter {assessment['encounter_id']}: probability "
            f"{prediction['risk_probability']:.3f}, label {prediction['readmission_risk']} at threshold "
            f"{prediction['risk_threshold']:.2f}; model version {prediction['model_version']}.")
    item = _evidence(ctx, source, text, "model_output", "Readmission model output")
    item.evidence_id = f"model:{assessment['encounter_id']}"
    return data, [item]


def _explanations(ctx: ToolContext, _: BaseModel) -> tuple[Any, list[EvidenceItem]]:
    data, evidence = _prediction(ctx, NoArgs())
    if not data["available"]:
        return data, evidence
    features = data["prediction"]["top_features"]
    text = "Top model contributions (SHAP): " + "; ".join(
        f"{f['feature']} {'raises' if f['contribution'] > 0 else 'lowers'} the score ({f['contribution']:+.3f})"
        for f in features)
    item = EvidenceItem(evidence_id=f"shap:{data['encounter_id']}", patient_id=ctx.patient_id,
                        source_ref=evidence[0].source_ref, source_version=evidence[0].source_version,
                        text=text, kind="model_explanation", title="SHAP contributions")
    return {**data, "top_features": features}, evidence + [item]


def _missing(ctx: ToolContext, _: BaseModel) -> tuple[Any, list[EvidenceItem]]:
    findings = ctx.records.missing_information(ctx.patient_id)
    evidence = []
    for index, finding in enumerate(findings, start=1):
        source = finding["source"]
        evidence.append(EvidenceItem(
            evidence_id=f"check:{finding['category']}:{index}", patient_id=ctx.patient_id,
            source_ref=source["reference"], source_version=str(source.get("version") or "current"),
            text=finding["message"], kind="completeness_check", title=finding["category"],
        ))
    return findings, evidence


def _contradictions(ctx: ToolContext, _: BaseModel) -> tuple[Any, list[EvidenceItem]]:
    findings = find_inconsistencies(ctx.records, ctx.patient_id)
    evidence = []
    for index, finding in enumerate(findings, start=1):
        for source in finding["sources"]:
            evidence.append(_evidence(ctx, source, f"{source['title']} ({_date(source['date'])}): "
                                                   f"\"{source['excerpt']}\"", "consistency_check",
                                      source["title"], source["date"]))
        evidence.append(EvidenceItem(evidence_id=f"check:inconsistency:{index}", patient_id=ctx.patient_id,
                                     source_ref=finding["sources"][0]["reference"],
                                     source_version=str(finding["sources"][0]["version"]),
                                     text=finding["message"], kind="consistency_check",
                                     title=finding["attribute"]))
    return findings, evidence


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec("get_patient_profile", "Demographics and record counts for the session patient.", NoArgs,
                 Action.PATIENT_READ, _profile),
        ToolSpec("get_patient_encounters", "All encounters (inpatient, ambulatory, emergency) with dates and "
                 "length of stay.", NoArgs, Action.PATIENT_READ, _encounters),
        ToolSpec("get_patient_conditions", "Recorded conditions / problem list with ICD-10 codes and onset.",
                 NoArgs, Action.PATIENT_READ, _conditions),
        ToolSpec("get_medication_history", "Medication orders including every recorded version and status "
                 "change.", NoArgs, Action.PATIENT_READ, _medications),
        ToolSpec("get_lab_observations", "Numeric laboratory results grouped by test with trends.", LabQuery,
                 Action.PATIENT_READ, _labs),
        ToolSpec("get_clinical_timeline", "Chronological merged timeline of encounters, orders, results and "
                 "documents.", NoArgs, Action.PATIENT_READ, _timeline),
        ToolSpec("retrieve_clinical_notes", "Hybrid search over the patient's notes and structured facts.",
                 NotesQuery, Action.RETRIEVAL_SEARCH, _notes),
        ToolSpec("get_readmission_prediction", "Readmission model output where the model applies to the "
                 "record; otherwise the reasons it does not.", NoArgs, Action.RISK_READ, _prediction,
                 timeout_s=15.0),
        ToolSpec("get_model_explanations", "SHAP feature contributions for the readmission prediction.", NoArgs,
                 Action.RISK_READ, _explanations, timeout_s=15.0),
        ToolSpec("detect_missing_information", "Documentation completeness checks (missing discharge "
                 "summaries, end times, units, medication lists).", NoArgs, Action.PATIENT_READ, _missing),
        ToolSpec("detect_inconsistencies", "Contradictory attributes and note values that match no recorded "
                 "observation.", NoArgs, Action.PATIENT_READ, _contradictions),
    )
}
PLANNER_TOOLS = tuple(name for name, spec in TOOLS.items() if spec.planner_visible)


def run_tool(ctx: ToolContext, name: str, raw_args: dict[str, Any] | None = None) -> ToolResult:
    started = time.perf_counter()

    def done(**kwargs: Any) -> ToolResult:
        return ToolResult(tool=name, latency_ms=(time.perf_counter() - started) * 1000, **kwargs)

    spec = TOOLS.get(name)
    if spec is None:
        return done(ok=False, error=f"unknown tool '{name}'", error_type="unknown_tool")
    try:
        args = spec.args_model.model_validate(raw_args or {})
    except ValidationError as exc:
        return done(ok=False, error=f"invalid arguments: {exc.error_count()} error(s)", error_type="invalid_args")
    try:
        ctx.policy.require(ctx.access, spec.action, ctx.patient_id)
    except AuthorizationError as exc:
        return done(ok=False, error=str(exc), error_type="unauthorized")
    future = _EXECUTOR.submit(spec.handler, ctx, args)
    try:
        data, evidence = future.result(timeout=spec.timeout_s)
    except FutureTimeout:
        future.cancel()
        return done(ok=False, error=f"timed out after {spec.timeout_s}s", error_type="timeout")
    except LookupError as exc:
        return done(ok=False, error=str(exc), error_type="not_found")
    except Exception as exc:  # noqa: BLE001 - converted into a structured, non-leaking error
        return done(ok=False, error=type(exc).__name__, error_type="internal")
    foreign = [e for e in evidence if e.patient_id != ctx.patient_id]
    if foreign:
        return done(ok=False, error="tool produced evidence for another patient", error_type="isolation")
    return done(ok=True, data=data, evidence=evidence)


def tool_catalog() -> list[dict[str, Any]]:
    return [
        {"name": spec.name, "description": spec.description, "action": spec.action.value,
         "timeout_s": spec.timeout_s, "args_schema": spec.args_model.model_json_schema()}
        for spec in TOOLS.values()
    ]
