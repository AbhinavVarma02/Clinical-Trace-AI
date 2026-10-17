"""Patient-level clinical views built from stored FHIR resources.

Converts raw FHIR resources into compact, typed dictionaries used by the agent
tools, the API, and the UI. Every item keeps a ``source`` block
(``reference``, ``version``, ``last_updated``) so any statement derived from it
can be cited back to the exact resource version.
"""

from __future__ import annotations

from typing import Any

from src.clinical.fhir_models import (
    decode_attachment,
    display_text,
    first_code,
    normalize_timestamp,
    parse_timestamp,
)
from src.clinical.store import FHIRStore


def source_of(resource: dict[str, Any]) -> dict[str, Any]:
    meta = resource.get("meta") or {}
    return {
        "reference": f"{resource['resourceType']}/{resource['id']}",
        "version": str(meta.get("versionId", "1")),
        "last_updated": normalize_timestamp(str(meta.get("lastUpdated"))) if meta.get("lastUpdated") else None,
        "synthetic": any(tag.get("code") == "synthetic" for tag in meta.get("tag") or []),
    }


def _age_on(birth_date: str, when: str) -> int:
    born = parse_timestamp(birth_date)
    at = parse_timestamp(when)
    years = at.year - born.year - ((at.month, at.day) < (born.month, born.day))
    return max(years, 0)


def _encounter_ref(resource: dict[str, Any]) -> str | None:
    ref = (resource.get("encounter") or {}).get("reference")
    if ref:
        return str(ref)
    context = (resource.get("context") or {}).get("encounter") or []
    return str(context[0].get("reference")) if context else None


class ClinicalRecords:
    """Read-only, patient-scoped views. Callers must authorise access first."""

    def __init__(self, store: FHIRStore) -> None:
        self.store = store

    # ---------------------------------------------------------------- profile
    def profile(self, patient_id: str) -> dict[str, Any] | None:
        patient = self.store.get("Patient", patient_id)
        if patient is None:
            return None
        name = (patient.get("name") or [{}])[0]
        encounters = self.encounters(patient_id)
        last = encounters[-1]["start"] if encounters else None
        return {
            "patient_id": patient_id,
            "display_name": name.get("text") or f"{' '.join(name.get('given', []))} {name.get('family', '')}",
            "gender": patient.get("gender"),
            "birth_date": patient.get("birthDate"),
            "age_at_last_encounter": _age_on(patient["birthDate"], last) if last else None,
            "synthetic": source_of(patient)["synthetic"],
            "counts": {
                rtype: self.store.search(patient_id, rtype, limit=1)[1]
                for rtype in ("Encounter", "Condition", "Observation", "MedicationRequest",
                              "DocumentReference", "DiagnosticReport")
            },
            "freshness": self.store.freshness(patient_id),
            "source": source_of(patient),
        }

    # ------------------------------------------------------------- encounters
    def encounters(self, patient_id: str) -> list[dict[str, Any]]:
        items = []
        for enc in self.store.all_for_patient(patient_id, "Encounter"):
            period = enc.get("period") or {}
            start = normalize_timestamp(period["start"]) if period.get("start") else None
            end = normalize_timestamp(period["end"]) if period.get("end") else None
            length_days = None
            if start and end:
                delta = parse_timestamp(end).date() - parse_timestamp(start).date()
                length_days = max(delta.days, 0)
            items.append({
                "id": enc["id"],
                "class": (enc.get("class") or {}).get("code"),
                "class_display": (enc.get("class") or {}).get("display"),
                "start": start,
                "end": end,
                "length_of_stay_days": length_days,
                "reason": display_text((enc.get("reasonCode") or [{}])[0]),
                "admit_source": display_text((enc.get("hospitalization") or {}).get("admitSource")),
                "status": enc.get("status"),
                "source": source_of(enc),
            })
        return sorted(items, key=lambda e: e["start"] or "")

    # ------------------------------------------------------------- conditions
    def conditions(self, patient_id: str) -> list[dict[str, Any]]:
        items = []
        for cond in self.store.all_for_patient(patient_id, "Condition"):
            system, code = first_code(cond.get("code"))
            status_codes = (cond.get("clinicalStatus") or {}).get("coding") or [{}]
            items.append({
                "id": cond["id"],
                "code": code,
                "code_system": system,
                "display": display_text(cond.get("code")),
                "clinical_status": status_codes[0].get("code"),
                "onset": normalize_timestamp(cond["onsetDateTime"]) if cond.get("onsetDateTime") else None,
                "encounter": _encounter_ref(cond),
                "source": source_of(cond),
            })
        return sorted(items, key=lambda c: c["onset"] or "")

    # ------------------------------------------------------------ medications
    def medications(self, patient_id: str, include_history: bool = True) -> list[dict[str, Any]]:
        items = []
        for med in self.store.all_for_patient(patient_id, "MedicationRequest"):
            entry = self._medication_entry(med)
            if include_history:
                entry["history"] = [
                    {k: v for k, v in self._medication_entry(version).items() if k != "history"}
                    for version in self.store.history("MedicationRequest", med["id"])
                ]
            items.append(entry)
        return sorted(items, key=lambda m: m["authored_on"] or "")

    @staticmethod
    def _medication_entry(med: dict[str, Any]) -> dict[str, Any]:
        dosage = (med.get("dosageInstruction") or [{}])[0].get("text")
        notes = [n.get("text") for n in med.get("note") or [] if n.get("text")]
        return {
            "id": med["id"],
            "medication": display_text(med.get("medicationCodeableConcept")),
            "status": med.get("status"),
            "dosage": dosage,
            "authored_on": normalize_timestamp(med["authoredOn"]) if med.get("authoredOn") else None,
            "encounter": _encounter_ref(med),
            "notes": notes,
            "source": source_of(med),
        }

    # ------------------------------------------------------------ observations
    def observations(self, patient_id: str, category: str | None = None) -> list[dict[str, Any]]:
        items = []
        for obs in self.store.all_for_patient(patient_id, "Observation"):
            categories = [
                coding.get("code")
                for cat in obs.get("category") or []
                for coding in cat.get("coding") or []
            ]
            if category and category not in categories:
                continue
            quantity = obs.get("valueQuantity") or {}
            _, loinc = first_code(obs.get("code"))
            items.append({
                "id": obs["id"],
                "loinc": loinc,
                "name": display_text(obs.get("code")),
                "category": categories[0] if categories else None,
                "value": quantity.get("value", obs.get("valueString")),
                "unit": quantity.get("unit"),
                "unit_missing": bool(quantity) and not quantity.get("unit"),
                "effective": normalize_timestamp(obs["effectiveDateTime"]),
                "encounter": _encounter_ref(obs),
                "source": source_of(obs),
            })
        return sorted(items, key=lambda o: o["effective"])

    def lab_trends(self, patient_id: str) -> list[dict[str, Any]]:
        """Group numeric laboratory observations by test, oldest first."""
        series: dict[str, dict[str, Any]] = {}
        for obs in self.observations(patient_id, category="laboratory"):
            if not isinstance(obs["value"], (int, float)):
                continue
            entry = series.setdefault(obs["loinc"], {"loinc": obs["loinc"], "name": obs["name"],
                                                     "unit": obs["unit"], "points": []})
            entry["points"].append({"effective": obs["effective"], "value": obs["value"],
                                    "unit": obs["unit"], "source": obs["source"]})
            if not entry["unit"] and obs["unit"]:
                entry["unit"] = obs["unit"]
        for entry in series.values():
            points = entry["points"]
            entry["count"] = len(points)
            if len(points) >= 2:
                first, last = points[0]["value"], points[-1]["value"]
                entry["direction"] = "up" if last > first else "down" if last < first else "flat"
                entry["change"] = round(last - first, 3)
            else:
                entry["direction"] = None
                entry["change"] = None
        return sorted(series.values(), key=lambda s: s["name"])

    # -------------------------------------------------------------- documents
    def documents(self, patient_id: str) -> list[dict[str, Any]]:
        items = []
        for doc in self.store.all_for_patient(patient_id, "DocumentReference"):
            items.append({
                "id": doc["id"],
                "type": display_text(doc.get("type")),
                "title": doc.get("description") or display_text(doc.get("type")),
                "date": normalize_timestamp(doc["date"]),
                "author": ((doc.get("author") or [{}])[0]).get("display"),
                "encounter": _encounter_ref(doc),
                "source": source_of(doc),
            })
        return sorted(items, key=lambda d: d["date"])

    def document_text(self, patient_id: str, document_id: str) -> dict[str, Any] | None:
        doc = self.store.get("DocumentReference", document_id)
        if doc is None or (doc.get("subject") or {}).get("reference") != f"Patient/{patient_id}":
            return None
        meta = next(d for d in self.documents(patient_id) if d["id"] == document_id)
        return {**meta, "text": decode_attachment(doc)}

    def diagnostic_reports(self, patient_id: str) -> list[dict[str, Any]]:
        return [
            {
                "id": rep["id"],
                "name": display_text(rep.get("code")),
                "effective": normalize_timestamp(rep["effectiveDateTime"]),
                "conclusion": rep.get("conclusion"),
                "results": [r.get("reference") for r in rep.get("result") or []],
                "encounter": _encounter_ref(rep),
                "source": source_of(rep),
            }
            for rep in self.store.all_for_patient(patient_id, "DiagnosticReport")
        ]

    # --------------------------------------------------------------- timeline
    def timeline(self, patient_id: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for enc in self.encounters(patient_id):
            label = f"{enc['class_display'] or enc['class']}: {enc['reason']}"
            if enc["length_of_stay_days"] is not None and enc["class"] == "IMP":
                label += f" ({enc['length_of_stay_days']} days)"
            events.append({"when": enc["start"], "kind": "encounter", "label": label,
                           "detail": enc, "source": enc["source"]})
        for cond in self.conditions(patient_id):
            if cond["onset"]:
                events.append({"when": cond["onset"], "kind": "condition",
                               "label": f"Condition recorded: {cond['display']}", "detail": cond,
                               "source": cond["source"]})
        for med in self.medications(patient_id):
            for version in med["history"]:
                when = version["source"]["last_updated"] if version["source"]["version"] != "1" \
                    else version["authored_on"]
                events.append({"when": when, "kind": "medication",
                               "label": f"{version['medication']} - {version['status']}"
                                        f" ({version['dosage']})",
                               "detail": version, "source": version["source"]})
        for obs in self.observations(patient_id):
            value = f"{obs['value']} {obs['unit'] or ''}".strip()
            events.append({"when": obs["effective"], "kind": obs["category"] or "observation",
                           "label": f"{obs['name']}: {value}", "detail": obs, "source": obs["source"]})
        for doc in self.documents(patient_id):
            events.append({"when": doc["date"], "kind": "document", "label": doc["title"],
                           "detail": doc, "source": doc["source"]})
        return sorted(events, key=lambda e: (e["when"] or "", e["kind"]))

    # ------------------------------------------------- missing information
    def missing_information(self, patient_id: str) -> list[dict[str, Any]]:
        """Deterministic documentation-completeness checks with cited sources."""
        patient = self.store.get("Patient", patient_id)
        if patient is None:
            return []
        findings: list[dict[str, Any]] = []
        documents = self.documents(patient_id)
        discharge_encounters = {d["encounter"] for d in documents if d["type"] == "Discharge summary"}
        for enc in self.encounters(patient_id):
            if enc["class"] == "IMP" and not enc["end"]:
                findings.append({"category": "encounter_end_missing", "severity": "high",
                                 "message": f"Inpatient encounter {enc['id']} has no discharge/end time.",
                                 "source": enc["source"]})
            if enc["class"] == "IMP" and f"Encounter/{enc['id']}" not in discharge_encounters:
                findings.append({"category": "discharge_summary_missing", "severity": "high",
                                 "message": f"No discharge summary found for inpatient encounter {enc['id']}.",
                                 "source": enc["source"]})
        for obs in self.observations(patient_id):
            if obs["unit_missing"]:
                findings.append({"category": "unit_missing", "severity": "medium",
                                 "message": f"{obs['name']} value {obs['value']} has no unit recorded.",
                                 "source": obs["source"]})
        if not self.medications(patient_id, include_history=False):
            findings.append({"category": "medication_list_missing", "severity": "high",
                             "message": "No medication orders are recorded for this patient.",
                             "source": source_of(patient)})
        for doc in documents:
            text = (self.document_text(patient_id, doc["id"]) or {}).get("text", "").lower()
            for marker in ("[documentation incomplete]", "not documented", "not completed", "not listed"):
                if marker in text:
                    findings.append({"category": "note_incomplete", "severity": "medium",
                                     "message": f"'{doc['title']}' contains the phrase \"{marker}\".",
                                     "source": doc["source"]})
        return findings
