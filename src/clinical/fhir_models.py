"""Structural validation and normalisation for the supported FHIR R4 resources.

This is a deliberately small validator for the subset of R4 the platform reads
(Patient, Encounter, Condition, Observation, MedicationRequest,
DocumentReference, DiagnosticReport). It checks the fields the application
relies on rather than the full R4 specification, and it never mutates input.
"""

from __future__ import annotations

import base64
import binascii
import re
from datetime import date, datetime, timezone
from typing import Any

SUPPORTED_TYPES = (
    "Patient",
    "Encounter",
    "Condition",
    "Observation",
    "MedicationRequest",
    "DocumentReference",
    "DiagnosticReport",
)
ID_PATTERN = re.compile(r"^[A-Za-z0-9\-.]{1,64}$|^synthetic_[a-z0-9]{1,32}$")
MAX_ATTACHMENT_BYTES = 64 * 1024
ALLOWED_ATTACHMENT_TYPES = ("text/plain",)

REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "Patient": ("gender", "birthDate"),
    "Encounter": ("status", "class", "subject", "period"),
    "Condition": ("code", "subject"),
    "Observation": ("status", "code", "subject", "effectiveDateTime"),
    "MedicationRequest": ("status", "intent", "medicationCodeableConcept", "subject", "authoredOn"),
    "DocumentReference": ("status", "type", "subject", "date", "content"),
    "DiagnosticReport": ("status", "code", "subject", "effectiveDateTime"),
}
DATE_FIELDS: dict[str, tuple[str, ...]] = {
    "Encounter": ("period.start", "period.end"),
    "Condition": ("onsetDateTime", "recordedDate"),
    "Observation": ("effectiveDateTime",),
    "MedicationRequest": ("authoredOn",),
    "DocumentReference": ("date",),
    "DiagnosticReport": ("effectiveDateTime",),
}


class FHIRValidationError(ValueError):
    def __init__(self, issues: list[str]) -> None:
        super().__init__("; ".join(issues))
        self.issues = issues


def _get(resource: dict[str, Any], dotted: str) -> Any:
    value: Any = resource
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def parse_timestamp(value: str) -> datetime:
    """Parse FHIR date/dateTime/instant values into an aware UTC datetime."""
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        parsed_date = date.fromisoformat(text)
        return datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_timestamp(value: str) -> str:
    """Return an ISO-8601 UTC string (``...Z``) for any supported FHIR time value."""
    return parse_timestamp(value).isoformat().replace("+00:00", "Z")


def patient_id_of(resource: dict[str, Any]) -> str | None:
    if resource.get("resourceType") == "Patient":
        return resource.get("id")
    reference = _get(resource, "subject.reference")
    if isinstance(reference, str) and reference.startswith("Patient/"):
        return reference.split("/", 1)[1]
    return None


def decode_attachment(resource: dict[str, Any]) -> str:
    """Safely decode the first plain-text attachment of a DocumentReference."""
    content = resource.get("content") or []
    if not content:
        raise FHIRValidationError(["DocumentReference has no content"])
    attachment = content[0].get("attachment") or {}
    content_type = str(attachment.get("contentType", "")).split(";")[0].strip().lower()
    if content_type not in ALLOWED_ATTACHMENT_TYPES:
        raise FHIRValidationError([f"unsupported attachment contentType '{content_type}'"])
    data = attachment.get("data")
    if not isinstance(data, str):
        raise FHIRValidationError(["attachment has no inline data"])
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FHIRValidationError(["attachment data is not valid base64"]) from exc
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise FHIRValidationError(["attachment exceeds size limit"])
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FHIRValidationError(["attachment is not valid UTF-8"]) from exc
    # Strip control characters other than newline/tab.
    return "".join(ch for ch in text if ch in "\n\t" or ch.isprintable())


def validate_resource(resource: Any) -> list[str]:
    """Return a list of validation issues; an empty list means valid."""
    if not isinstance(resource, dict):
        return ["resource is not an object"]
    issues: list[str] = []
    resource_type = resource.get("resourceType")
    if resource_type not in SUPPORTED_TYPES:
        return [f"unsupported resourceType '{resource_type}'"]
    resource_id = resource.get("id")
    if not isinstance(resource_id, str) or not ID_PATTERN.match(resource_id):
        issues.append("missing or invalid id")
    meta = resource.get("meta") or {}
    version = meta.get("versionId")
    if not (isinstance(version, str) and version.isdigit() and int(version) >= 1):
        issues.append("meta.versionId must be a positive integer string")
    if not meta.get("lastUpdated"):
        issues.append("meta.lastUpdated is required")
    for field in REQUIRED_FIELDS[resource_type]:
        if _get(resource, field) in (None, "", [], {}):
            issues.append(f"missing required field '{field}'")
    if resource_type != "Patient":
        pid = patient_id_of(resource)
        if not pid:
            issues.append("subject must reference a Patient")
    elif not str(resource_id or "").startswith("synthetic_"):
        issues.append("Patient id must be a synthetic identifier (synthetic_*)")
    for field in DATE_FIELDS.get(resource_type, ()):
        value = _get(resource, field)
        if value is None:
            continue
        try:
            parse_timestamp(str(value))
        except ValueError:
            issues.append(f"field '{field}' is not a valid FHIR date/dateTime")
    if meta.get("lastUpdated"):
        try:
            parse_timestamp(str(meta["lastUpdated"]))
        except ValueError:
            issues.append("meta.lastUpdated is not a valid instant")
    if resource_type == "DocumentReference" and not issues:
        try:
            decode_attachment(resource)
        except FHIRValidationError as exc:
            issues.extend(exc.issues)
    return issues


def require_valid(resource: Any) -> dict[str, Any]:
    issues = validate_resource(resource)
    if issues:
        raise FHIRValidationError(issues)
    return resource


def display_text(concept: dict[str, Any] | None) -> str:
    """Human-readable text for a CodeableConcept."""
    if not concept:
        return ""
    if concept.get("text"):
        return str(concept["text"])
    for coding in concept.get("coding") or []:
        if coding.get("display"):
            return str(coding["display"])
    return ""


def first_code(concept: dict[str, Any] | None) -> tuple[str, str]:
    for coding in (concept or {}).get("coding") or []:
        return str(coding.get("system", "")), str(coding.get("code", ""))
    return "", ""
