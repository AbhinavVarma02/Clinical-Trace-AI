"""Metadata-aware chunking of clinical documents and structured resources.

Notes are split on their section headers (``Assessment:``, ``Plan at
discharge:`` ...) so a citation points at one coherent section. Structured
resources (labs, medications, conditions, encounters) become one short "fact"
chunk each, so retrieval can surface them alongside narrative text.

Chunk ids embed the source version (``p001-d3#s2@v1``); a new resource version
produces new chunk ids, which keeps old citations reconstructable.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from src.clinical.records import ClinicalRecords

SECTION_HEADER = re.compile(r"^([A-Z][A-Za-z /\-]{2,40}):\s*(.*)$")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    patient_id: str
    source_ref: str
    source_version: str
    source_type: str
    title: str
    section: str
    date: str
    text: str
    char_start: int
    char_end: int
    content_hash: str
    flags: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["flags"] = list(self.flags)
        return data


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def split_sections(text: str) -> list[tuple[str, str, int, int]]:
    """Return (section, text, start, end) tuples using header lines."""
    sections: list[tuple[str, str, int, int]] = []
    offset = 0
    current_name, current_lines, current_start = "Text", [], 0
    for line in text.splitlines(keepends=True):
        match = SECTION_HEADER.match(line.strip())
        if match and current_lines:
            body = "".join(current_lines).strip()
            if body:
                sections.append((current_name, body, current_start, offset))
            current_lines = []
        if match:
            current_name, current_start = match.group(1), offset
        current_lines.append(line)
        offset += len(line)
    body = "".join(current_lines).strip()
    if body:
        sections.append((current_name, body, current_start, offset))
    return sections


def chunk_patient(records: ClinicalRecords, patient_id: str, flagger: Any = None) -> list[Chunk]:
    """Build all chunks for one patient. ``flagger(text) -> list[str]`` adds safety flags."""
    chunks: list[Chunk] = []

    def add(**kwargs: Any) -> None:
        flags = tuple(flagger(kwargs["text"])) if flagger else ()
        chunks.append(Chunk(content_hash=_hash(kwargs["text"]), flags=flags, patient_id=patient_id, **kwargs))

    for doc in records.documents(patient_id):
        full = records.document_text(patient_id, doc["id"])
        if not full:
            continue
        version = doc["source"]["version"]
        for index, (section, body, start, end) in enumerate(split_sections(full["text"]), start=1):
            add(chunk_id=f"{doc['id']}#s{index}@v{version}", source_ref=doc["source"]["reference"],
                source_version=version, source_type=doc["type"], title=doc["title"], section=section,
                date=doc["date"], text=body, char_start=start, char_end=end)

    for obs in records.observations(patient_id):
        value = f"{obs['value']} {obs['unit'] or '(no unit recorded)'}" if obs["unit_missing"] \
            else f"{obs['value']} {obs['unit'] or ''}".strip()
        text = f"{obs['name']} {value} on {obs['effective'][:10]}."
        add(chunk_id=f"{obs['id']}@v{obs['source']['version']}", source_ref=obs["source"]["reference"],
            source_version=obs["source"]["version"], source_type=f"Observation ({obs['category']})",
            title=obs["name"], section="Result", date=obs["effective"], text=text,
            char_start=0, char_end=len(text))

    for med in records.medications(patient_id):
        for version in med["history"]:
            notes = f" Note: {' '.join(version['notes'])}" if version["notes"] else ""
            text = (f"Medication order {version['medication']}, {version['dosage']}, status {version['status']}, "
                    f"ordered {(version['authored_on'] or '')[:10]}.{notes}")
            add(chunk_id=f"{med['id']}@v{version['source']['version']}", source_ref=version["source"]["reference"],
                source_version=version["source"]["version"], source_type="MedicationRequest",
                title=version["medication"], section="Order",
                date=version["source"]["last_updated"] or version["authored_on"] or "", text=text,
                char_start=0, char_end=len(text))

    for cond in records.conditions(patient_id):
        text = f"Condition {cond['display']} (ICD-10 {cond['code']}), status {cond['clinical_status']}, " \
               f"onset {(cond['onset'] or 'unknown')[:10]}."
        add(chunk_id=f"{cond['id']}@v{cond['source']['version']}", source_ref=cond["source"]["reference"],
            source_version=cond["source"]["version"], source_type="Condition", title=cond["display"],
            section="Problem list", date=cond["onset"] or "", text=text, char_start=0, char_end=len(text))

    for enc in records.encounters(patient_id):
        stay = f", length of stay {enc['length_of_stay_days']} days" if enc["length_of_stay_days"] is not None else \
            ", no end time recorded"
        text = (f"{enc['class_display'].removesuffix(' encounter')} encounter for {enc['reason']} starting {(enc['start'] or '')[:10]}"
                f"{stay}.")
        add(chunk_id=f"{enc['id']}@v{enc['source']['version']}", source_ref=enc["source"]["reference"],
            source_version=enc["source"]["version"], source_type="Encounter", title=enc["reason"],
            section="Encounter", date=enc["start"] or "", text=text, char_start=0, char_end=len(text))
    return chunks
