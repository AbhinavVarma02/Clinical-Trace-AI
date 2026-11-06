"""Normalise Epic sandbox FHIR R4 resources to the existing Clinical-Trace contract.

Rules (see ``docs/EPIC_SANDBOX.md``, "Provenance and field status"):

* The existing ``validate_resource`` is the conformance gate; it is never loosened.
  Mapped output that fails it is rejected with issue codes.
* Every top-level source field is either copied verbatim (``original``), changed in
  format only (``transformed``), filled from a *different field of the same source
  resource* (``substituted``), recorded as absent (``unavailable``), or left out
  (``unsupported``). Statuses live in the ``urn:clinical-trace:epic:source`` extension
  with the original Epic reference, version, timestamps and a content hash.
* Clinical times come only from the source field with the same meaning. Retrieval
  time is never used as a clinical time. Where the store contract needs a record
  instant and Epic supplies none, ``meta.lastUpdated`` holds the retrieval time and
  is labelled as record metadata only.
* Every resource is stored as version "1" of an immutable snapshot; Epic's version
  id is kept in provenance only.
* Nothing clinical is invented: when a contract-required clinical value is missing
  or has a different meaning, the resource is rejected and counted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Mapping

from src.clinical.fhir_client import PatientScopeViolation
from src.clinical.fhir_models import MAX_ATTACHMENT_BYTES, parse_timestamp, validate_resource
from src.interop.epic import settings
from src.interop.epic.client import BinaryContent, subject_patient_id

MAPPING_VERSION = "epic-sandbox-map/1"
PROVENANCE_URL = "urn:clinical-trace:epic:source"
DATA_ORIGIN_TAG = {
    "system": "urn:clinical-trace:data-origin",
    "code": "synthetic",
    "display": "Epic on FHIR sandbox test record - not a real patient",
}
SOURCE_TAG = {
    "system": "urn:clinical-trace:source",
    "code": settings.SOURCE_SYSTEM,
    "display": "Epic on FHIR public R4 sandbox",
}

ICD10_SYSTEMS = ("http://hl7.org/fhir/sid/icd-10-cm", "http://hl7.org/fhir/sid/icd-10")
LOINC_SYSTEM = "http://loinc.org"
OBSERVATION_CATEGORY_SYSTEM = "http://terminology.hl7.org/CodeSystem/observation-category"

INTERNAL_PREFIX = {
    "Encounter": "enc",
    "Condition": "cond",
    "Observation": "obs",
    "MedicationRequest": "med",
    "DocumentReference": "doc",
    "DiagnosticReport": "dxr",
}
MAPPED_TYPES = ("Patient", *INTERNAL_PREFIX)

# Top-level fields copied (after reference rewriting) per type; anything else is "unsupported".
COPIED_FIELDS: dict[str, tuple[str, ...]] = {
    "Patient": ("name", "gender", "birthDate", "deceasedBoolean", "deceasedDateTime"),
    "Encounter": ("status", "class", "type", "serviceType", "priority", "period", "reasonCode", "hospitalization"),
    "Condition": (
        "clinicalStatus",
        "verificationStatus",
        "category",
        "code",
        "onsetDateTime",
        "onsetString",
        "abatementDateTime",
        "abatementString",
        "recordedDate",
        "encounter",
        "note",
    ),
    "Observation": (
        "status",
        "category",
        "code",
        "effectiveDateTime",
        "issued",
        "valueQuantity",
        "valueString",
        "valueCodeableConcept",
        "valueBoolean",
        "valueInteger",
        "component",
        "interpretation",
        "referenceRange",
        "encounter",
        "note",
    ),
    "MedicationRequest": (
        "status",
        "intent",
        "category",
        "medicationCodeableConcept",
        "authoredOn",
        "dosageInstruction",
        "courseOfTherapyType",
        "encounter",
        "note",
    ),
    "DocumentReference": ("status", "docStatus", "type", "category", "date", "description", "author", "context"),
    "DiagnosticReport": (
        "status",
        "category",
        "code",
        "effectiveDateTime",
        "issued",
        "result",
        "conclusion",
        "encounter",
    ),
}
_MANAGED = frozenset({"resourceType", "id", "meta", "subject"})
_REFERENCE = re.compile(r"^([A-Z][A-Za-z]+)/([A-Za-z0-9.\-]{1,256})$")


# --------------------------------------------------------------------------- identities
def internal_id(resource_type: str, source_id: str) -> str:
    digest = hashlib.sha256(f"{settings.SOURCE_SYSTEM}|{resource_type}|{source_id}".encode()).hexdigest()
    if resource_type == "Patient":
        return f"synthetic_epic{digest[:12]}"
    return f"epic-{INTERNAL_PREFIX[resource_type]}-{digest[:16]}"


def content_sha256(value: Any) -> str:
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _is_full_time(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parse_timestamp(value)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------- note text
class _HTMLText(HTMLParser):
    BLOCK = frozenset(
        {
            "p",
            "div",
            "br",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "table",
            "ul",
            "ol",
            "section",
            "pre",
            "blockquote",
            "hr",
        }
    )
    SKIP = frozenset({"script", "style", "head", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _HTMLText()
    parser.feed(markup)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    text = "\n".join(line for line in lines if line)
    return "".join(ch for ch in text if ch in "\n\t" or ch.isprintable())


def _decode_text(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# --------------------------------------------------------------------------- results
@dataclass
class MappedResource:
    source_type: str
    source_id: str
    resource: dict[str, Any] | None
    issues: list[str] = field(default_factory=list)
    statuses: dict[str, list[str]] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.resource is not None


@dataclass
class MappingReport:
    received: Counter = field(default_factory=Counter)
    mapped: Counter = field(default_factory=Counter)
    rejected: Counter = field(default_factory=Counter)  # "Type:issue-code"
    field_status: Counter = field(default_factory=Counter)  # "transformed" / "substituted" / ...
    skipped_types: dict[str, str] = field(default_factory=dict)
    dangling_references: int = 0

    def add(self, outcome: MappedResource) -> None:
        self.received[outcome.source_type] += 1
        if outcome.accepted:
            self.mapped[outcome.source_type] += 1
            for status, entries in outcome.statuses.items():
                self.field_status[status] += len(entries)
        else:
            for issue in outcome.issues:
                self.rejected[f"{outcome.source_type}:{issue}"] += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "mapping_version": MAPPING_VERSION,
            "received": dict(sorted(self.received.items())),
            "mapped": dict(sorted(self.mapped.items())),
            "rejected": dict(sorted(self.rejected.items())),
            "field_status": dict(sorted(self.field_status.items())),
            "skipped_types": dict(sorted(self.skipped_types.items())),
            "dangling_references": self.dangling_references,
        }


# --------------------------------------------------------------------------- mapper
class EpicMapper:
    """Maps one patient's Epic sandbox resources for one immutable snapshot."""

    def __init__(self, source_patient_id: str, *, snapshot_id: str, retrieved_at: str) -> None:
        if not _is_full_time(retrieved_at):
            raise ValueError("retrieved_at must be a full ISO-8601 instant")
        self.source_patient_id = source_patient_id
        self.patient_id = internal_id("Patient", source_patient_id)
        self.snapshot_id = snapshot_id
        self.retrieved_at = retrieved_at

    # ------------------------------------------------------------------ helpers
    def _rewrite_reference(self, value: Any, path: str, statuses: dict[str, list[str]]) -> Any:
        """Rewrite Epic references: mapped types -> internal ids, others -> absolute Epic URLs."""
        if isinstance(value, list):
            return [self._rewrite_reference(item, f"{path}[{i}]", statuses) for i, item in enumerate(value)]
        if not isinstance(value, dict):
            return value
        rewritten = {}
        for key, item in value.items():
            if key == "reference" and isinstance(item, str):
                rewritten[key] = self._reference(item, f"{path}.reference", statuses)
            else:
                rewritten[key] = self._rewrite_reference(item, f"{path}.{key}" if path else key, statuses)
        return rewritten

    def _reference(self, reference: str, path: str, statuses: dict[str, list[str]]) -> str:
        relative = (
            reference[len(settings.FHIR_BASE) + 1 :] if reference.startswith(settings.FHIR_BASE + "/") else reference
        )
        match = _REFERENCE.fullmatch(relative)
        if not match:
            return reference  # contained ("#x") or already absolute elsewhere: left as the source wrote it
        rtype, rid = match.groups()
        if rtype in MAPPED_TYPES:
            statuses.setdefault("transformed", []).append(f"{path}:internal-id")
            return f"{rtype}/{internal_id(rtype, rid)}"
        statuses.setdefault("transformed", []).append(f"{path}:absolute-source-url")
        return f"{settings.FHIR_BASE}/{relative}"

    def _provenance(self, source: dict[str, Any], statuses: dict[str, list[str]], content_hash: str) -> dict:
        meta: dict[str, Any] = source["meta"] if isinstance(source.get("meta"), dict) else {}
        parts: list[dict[str, Any]] = [
            {"url": "sourceReference", "valueString": f"{source['resourceType']}/{source['id']}"},
        ]
        if isinstance(meta.get("versionId"), str):
            parts.append({"url": "sourceVersionId", "valueString": meta["versionId"]})
        if _is_full_time(meta.get("lastUpdated")):
            parts.append({"url": "sourceLastUpdated", "valueInstant": meta["lastUpdated"]})
        parts += [
            {"url": "sourceContentSha256", "valueString": content_hash},
            {"url": "retrievedAt", "valueInstant": self.retrieved_at},
            {"url": "snapshotId", "valueString": self.snapshot_id},
            {"url": "mappingVersion", "valueString": MAPPING_VERSION},
        ]
        for status in ("transformed", "substituted", "unavailable", "unsupported"):
            parts += [{"url": status, "valueString": entry} for entry in statuses.get(status, [])]
        return {"url": PROVENANCE_URL, "extension": parts}

    # ------------------------------------------------------------------ entry point
    def map(self, source: dict[str, Any], binaries: Mapping[str, BinaryContent] | None = None) -> MappedResource:
        rtype = source.get("resourceType")
        sid = source.get("id")
        if rtype not in MAPPED_TYPES or not isinstance(sid, str):
            return MappedResource(str(rtype), str(sid), None, ["unsupported-resource-type"])
        if rtype == "Patient":
            if sid != self.source_patient_id:
                raise PatientScopeViolation("Epic resource is outside the imported patient")
        elif subject_patient_id(source) != self.source_patient_id:
            raise PatientScopeViolation("Epic resource is outside the imported patient")

        statuses: dict[str, list[str]] = {"transformed": ["id:namespaced"]}
        if rtype != "Patient":
            statuses["transformed"].append("subject.reference:internal-id")
        issues: list[str] = []
        mapped: dict[str, Any] = {"resourceType": rtype, "id": internal_id(rtype, sid)}
        for key in COPIED_FIELDS[rtype]:
            if key in source:
                mapped[key] = self._rewrite_reference(source[key], key, statuses)
        unsupported = sorted(k for k in source if k not in _MANAGED and k not in COPIED_FIELDS[rtype])
        statuses["unsupported"] = list(unsupported)
        if rtype != "Patient":
            mapped["subject"] = {"reference": f"Patient/{self.patient_id}"}

        handler = getattr(self, f"_map_{rtype}")
        handler(source, mapped, statuses, issues, binaries or {})
        if issues:
            return MappedResource(rtype, sid, None, issues, statuses)

        meta: dict[str, Any] = source["meta"] if isinstance(source.get("meta"), dict) else {}
        if _is_full_time(meta.get("lastUpdated")):
            last_updated = meta["lastUpdated"]
        else:
            last_updated = self.retrieved_at
            statuses.setdefault("unavailable", []).append("meta.lastUpdated")
            statuses.setdefault("substituted", []).append(
                "meta.lastUpdated<-retrievedAt (record metadata only; not a clinical time or a source update)"
            )
        version_note = "meta.versionId:snapshot-version-1"
        if isinstance(meta.get("versionId"), str):
            version_note += " (source version kept in provenance)"
        statuses["transformed"].append(version_note)
        source_url = f"{settings.FHIR_BASE}/{rtype}/{sid}"
        if isinstance(meta.get("versionId"), str) and re.fullmatch(r"[A-Za-z0-9.\-]{1,64}", meta["versionId"]):
            source_url += f"/_history/{meta['versionId']}"
        mapped["meta"] = {
            "versionId": "1",
            "lastUpdated": last_updated,
            "source": source_url,
            "tag": [dict(DATA_ORIGIN_TAG), dict(SOURCE_TAG)],
        }
        statuses = {k: v for k, v in statuses.items() if v}
        mapped["extension"] = [self._provenance(source, statuses, content_sha256(source))]
        problems = validate_resource(mapped)
        if problems:
            codes = [f"contract:{re.sub(r'[^A-Za-z0-9 ._-]', '', p)[:80]}" for p in problems]
            return MappedResource(rtype, sid, None, codes, statuses)
        return MappedResource(rtype, sid, mapped, [], statuses)

    # ------------------------------------------------------------------ per type
    def _map_Patient(self, source, mapped, statuses, issues, _binaries) -> None:
        for name in ("gender", "birthDate"):
            if not source.get(name):
                statuses.setdefault("unavailable", []).append(name)
                issues.append(f"missing-{name}")
        birth = source.get("birthDate")
        if birth and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(birth)):
            issues.append("partial-birthDate")

    def _map_Encounter(self, source, mapped, statuses, issues, _binaries) -> None:
        period = source.get("period")
        if not isinstance(period, dict) or not (period.get("start") or period.get("end")):
            statuses.setdefault("unavailable", []).append("period")
            issues.append("missing-clinical-time")
        else:
            for part in ("start", "end"):
                if part in period and not _is_full_time(period[part]):
                    issues.append("partial-clinical-time")
        klass = source.get("class")
        if not isinstance(klass, dict) or not klass.get("code"):
            statuses.setdefault("unavailable", []).append("class")
            issues.append("missing-class")
        elif not klass.get("display"):
            mapped["class"] = {**mapped["class"], "display": str(klass["code"])}
            statuses.setdefault("substituted", []).append("class.display<-class.code")
        if not source.get("status"):
            issues.append("missing-status")

    def _map_Condition(self, source, mapped, statuses, issues, _binaries) -> None:
        self._prefer_coding(mapped, "code", ICD10_SYSTEMS, "icd10", statuses, require=True)
        onset = source.get("onsetDateTime")
        if onset is not None and not _is_full_time(onset):
            mapped.pop("onsetDateTime", None)
            mapped["onsetString"] = str(onset)
            statuses.setdefault("transformed", []).append("onsetDateTime:partial-date-moved-to-onsetString")
            statuses.setdefault("unavailable", []).append("onsetDateTime")
        recorded = source.get("recordedDate")
        if recorded is not None and not _is_full_time(recorded):
            mapped.pop("recordedDate", None)
            statuses.setdefault("unavailable", []).append("recordedDate (partial source date not expanded)")
        if not source.get("code"):
            issues.append("missing-code")

    def _map_Observation(self, source, mapped, statuses, issues, _binaries) -> None:
        if "effectiveDateTime" not in source and "effectiveInstant" in source:
            mapped["effectiveDateTime"] = source["effectiveInstant"]
            statuses.setdefault("transformed", []).append("effectiveInstant:as-effectiveDateTime")
            statuses["unsupported"] = [k for k in statuses["unsupported"] if k != "effectiveInstant"]
        effective = mapped.get("effectiveDateTime")
        if effective is None:
            statuses.setdefault("unavailable", []).append("effectiveDateTime")
            issues.append("effective-period-unsupported" if "effectivePeriod" in source else "missing-clinical-time")
        elif not _is_full_time(effective):
            issues.append("partial-clinical-time")
        self._prefer_coding(mapped, "code", (LOINC_SYSTEM,), "loinc", statuses, require=False)
        self._prefer_category(mapped, statuses)
        quantity = source.get("valueQuantity")
        has_value = (isinstance(quantity, dict) and quantity.get("value") is not None) or isinstance(
            source.get("valueString"), str
        )
        if not has_value:
            # The existing views read only valueQuantity.value or valueString, so other value
            # types are rendered as text from the same resource (format change, same meaning).
            text = None
            if isinstance(source.get("valueInteger"), int) and not isinstance(source.get("valueInteger"), bool):
                text, origin = str(source["valueInteger"]), "valueInteger"
            elif isinstance(source.get("valueBoolean"), bool):
                text, origin = ("true" if source["valueBoolean"] else "false"), "valueBoolean"
            elif "valueCodeableConcept" in source:
                text, origin = _display(source["valueCodeableConcept"]) or None, "valueCodeableConcept"
            elif isinstance(source.get("component"), list):
                text, origin = _components_text(source["component"]) or None, "component"
            if text:
                mapped["valueString"] = text
                statuses.setdefault("transformed", []).append(f"{origin}:rendered-as-valueString")
                has_value = True
        if not has_value:
            statuses.setdefault("unavailable", []).append("value[x]")
            issues.append("missing-value")
        if not source.get("code"):
            issues.append("missing-code")

    def _map_MedicationRequest(self, source, mapped, statuses, issues, _binaries) -> None:
        if not source.get("authoredOn"):
            statuses.setdefault("unavailable", []).append("authoredOn")
            issues.append("missing-clinical-time")
        elif not _is_full_time(source["authoredOn"]):
            issues.append("partial-clinical-time")
        if "medicationCodeableConcept" not in source:
            display = (
                (source.get("medicationReference") or {}).get("display")
                if isinstance(source.get("medicationReference"), dict)
                else None
            )
            if isinstance(display, str) and display.strip():
                mapped["medicationCodeableConcept"] = {"text": display.strip()}
                statuses.setdefault("substituted", []).append(
                    "medicationCodeableConcept.text<-medicationReference.display"
                )
                statuses["unsupported"] = [k for k in statuses["unsupported"] if k != "medicationReference"]
                statuses.setdefault("transformed", []).append("medicationReference:replaced-by-concept-text")
            else:
                statuses.setdefault("unavailable", []).append("medicationCodeableConcept")
                issues.append("medication-unidentified")

    def _map_DocumentReference(self, source, mapped, statuses, issues, binaries) -> None:
        if not _is_full_time(source.get("date")):
            statuses.setdefault("unavailable", []).append("date")
            issues.append("missing-clinical-time" if not source.get("date") else "partial-clinical-time")
        attachments: list[dict[str, Any]] = [
            c["attachment"]
            for c in source.get("content") or []
            if isinstance(c, dict) and isinstance(c.get("attachment"), dict)
        ]
        chosen: dict[str, Any] | None = None
        for preferred in ("text/html", "text/plain"):
            chosen = next(
                (a for a in attachments if str(a.get("contentType", "")).split(";")[0].strip().lower() == preferred),
                None,
            )
            if chosen:
                break
        if chosen is None:
            statuses.setdefault("unavailable", []).append("content (no HTML or plain-text attachment)")
            issues.append("note-format-unsupported")
            return
        kind = str(chosen.get("contentType", "")).split(";")[0].strip().lower()
        raw: bytes | None = None
        origin = "inline"
        if isinstance(chosen.get("data"), str):
            try:
                raw = base64.b64decode(chosen["data"], validate=True)
            except ValueError:
                raw = None
        elif isinstance(chosen.get("url"), str):
            ref = chosen["url"]
            ref = ref[len(settings.FHIR_BASE) + 1 :] if ref.startswith(settings.FHIR_BASE + "/") else ref
            binary = binaries.get(ref)
            raw, origin = (binary.data if binary else None), ref
        decoded = _decode_text(raw) if raw is not None else None
        if decoded is None:
            statuses.setdefault("unavailable", []).append("content (note text not retrieved)")
            issues.append("note-content-unavailable")
            return
        text = (
            html_to_text(decoded)
            if kind == "text/html"
            else "".join(ch for ch in decoded if ch in "\n\t" or ch.isprintable()).strip()
        )
        encoded = text.encode("utf-8")
        if not text:
            issues.append("note-empty")
            return
        if len(encoded) > MAX_ATTACHMENT_BYTES:
            issues.append("note-too-large")
            return
        attachment: dict[str, Any] = {"contentType": "text/plain", "data": base64.b64encode(encoded).decode("ascii")}
        if isinstance(chosen.get("title"), str):
            attachment["title"] = chosen["title"]
        mapped["content"] = [{"attachment": attachment}]
        conversion = "html-to-text" if kind == "text/html" else "plain-text-inlined"
        statuses.setdefault("transformed", []).append(
            f"content:{conversion} from {origin} (source sha256 {content_sha256(raw or b'')[:16]})"
        )
        statuses["unsupported"] = [k for k in statuses["unsupported"] if k != "content"]

    def _map_DiagnosticReport(self, source, mapped, statuses, issues, _binaries) -> None:
        effective = source.get("effectiveDateTime")
        if effective is None:
            statuses.setdefault("unavailable", []).append("effectiveDateTime")
            issues.append("effective-period-unsupported" if "effectivePeriod" in source else "missing-clinical-time")
        elif not _is_full_time(effective):
            issues.append("partial-clinical-time")
        if not source.get("code"):
            issues.append("missing-code")

    # ------------------------------------------------------------------ coding order
    @staticmethod
    def _prefer_coding(
        mapped: dict, key: str, systems: tuple[str, ...], label: str, statuses: dict[str, list[str]], *, require: bool
    ) -> None:
        concept: Any = mapped.get(key)
        codings = concept.get("coding") if isinstance(concept, dict) else None
        if not isinstance(codings, list) or not codings:
            return
        preferred = [c for c in codings if isinstance(c, dict) and c.get("system") in systems]
        if not preferred:
            if require:
                # Existing views label the first code as ICD-10; never assert a non-ICD-10 code as one.
                mapped[key] = {k: v for k, v in concept.items() if k != "coding"}
                if not _display(mapped[key]):
                    mapped[key]["text"] = next(
                        (str(c["display"]) for c in codings if isinstance(c, dict) and c.get("display")), ""
                    )
                statuses.setdefault("unsupported", []).append(
                    f"{key}.coding (no {label} coding; other code systems are not carried because the "
                    f"existing views label the first code as {label})"
                )
                statuses.setdefault("unavailable", []).append(f"{key} ({label} code)")
            return
        ordered = preferred + [c for c in codings if c not in preferred]
        if ordered != codings:
            mapped[key] = {**concept, "coding": ordered}
            statuses.setdefault("transformed", []).append(f"{key}.coding:reordered-{label}-first")

    @staticmethod
    def _prefer_category(mapped: dict, statuses: dict[str, list[str]]) -> None:
        categories = mapped.get("category")
        if not isinstance(categories, list) or len(categories) < 2:
            return

        def standard(concept: Any) -> bool:
            return isinstance(concept, dict) and any(
                isinstance(c, dict) and c.get("system") == OBSERVATION_CATEGORY_SYSTEM
                for c in concept.get("coding") or []
            )

        ordered = [c for c in categories if standard(c)] + [c for c in categories if not standard(c)]
        if ordered != categories:
            mapped["category"] = ordered
            statuses.setdefault("transformed", []).append("category:reordered-hl7-category-first")


def _display(concept: Any) -> str:
    if not isinstance(concept, dict):
        return ""
    if concept.get("text"):
        return str(concept["text"])
    for coding in concept.get("coding") or []:
        if isinstance(coding, dict) and coding.get("display"):
            return str(coding["display"])
    return ""


def _components_text(components: list[Any]) -> str:
    parts = []
    for component in components:
        if not isinstance(component, dict):
            continue
        name = _display(component.get("code"))
        quantity = component.get("valueQuantity")
        if not name or not isinstance(quantity, dict) or quantity.get("value") is None:
            continue
        unit = quantity.get("unit") or ""
        parts.append(f"{name} {quantity['value']} {unit}".strip())
    return "; ".join(parts)
