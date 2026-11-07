"""Data sources: one isolated application runtime per configured source (local only; off by default).

By default the application serves only the synthetic FHIR fixture, exactly as before. For local
testing, ``CLINICAL_TRACE_DATA_SOURCES`` can add Epic sandbox snapshots beside it::

    CLINICAL_TRACE_DATA_SOURCES=synthetic,epic-sandbox=<snapshot id or path under var/epic_sandbox/>

A request chooses a source with the optional ``X-Data-Source`` header. Without it, the request uses
the synthetic source. Each rule below fails closed when the sources are built:

* ``synthetic`` must be listed first, so requests without the header behave as before.
* Each source has its own runtime: database, retrieval index, agent, reviews and audit log. A
  patient id may appear in only one source.
* An Epic snapshot is only read. Its runtime database is in memory, so serving the snapshot never
  writes into it and nothing is kept after the process stops.
* Epic sources run deterministically. A configured language-model provider or LangSmith tracing
  stops the build: the language-model composer does not carry the source's coverage notes, and
  tracing would upload record content to a third party.
* Persona panels for an Epic source come from its snapshot manifest. The synthetic personas are
  unchanged.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Mapping

from src.safety.policy import ALL_SYNTHETIC_PATIENTS, PERSONAS, Persona, resolve_persona

if TYPE_CHECKING:  # pragma: no cover
    from src.runtime import Runtime

ENV_VAR = "CLINICAL_TRACE_DATA_SOURCES"
SYNTHETIC = "synthetic"
SYNTHETIC_KIND = "synthetic-fixture"
EPIC_KIND = "epic-sandbox-snapshot"
SYNTHETIC_ORIGIN = "synthetic FHIR R4 fixture (no real patients)"
# Must equal src.interop.epic.mapping.PROVENANCE_URL (a test checks this). Kept here so that the
# default, synthetic-only application never imports the Epic adapter.
EPIC_PROVENANCE_URL = "urn:clinical-trace:epic:source"
RETRIEVED_AT_NOTE = (
    "retrieved_at is when the snapshot was fetched from the source. It is never a clinical time or a "
    "source update time."
)
SYNTHETIC_COVERAGE_NOTE = (
    "The synthetic fixture declares no import coverage: every fixture resource is loaded as authored."
)

_SOURCE_ID = re.compile(r"[a-z][a-z0-9-]{1,39}")
_EPIC_SOURCE_ID = re.compile(r"epic-sandbox(?:-[a-z0-9]{1,16})?")
_TRACING_ENV = ("LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING", "LANGSMITH_TRACING", "LANGSMITH_TRACING_V2")
_PROVENANCE_FIELDS = {
    "sourceReference": "source_reference",
    "sourceVersionId": "source_version_id",
    "sourceLastUpdated": "source_last_updated",
    "sourceContentSha256": "source_content_sha256",
    "snapshotId": "snapshot_id",
    "mappingVersion": "mapping_version",
    "retrievedAt": "retrieved_at",
}
_FIELD_STATUSES = ("transformed", "substituted", "unavailable", "unsupported")


class SourceConfigError(RuntimeError):
    """The data-source configuration is invalid, so nothing is served. Messages hold no clinical content."""


class DataSourceError(ValueError):
    """A request named a data source that is malformed or not configured on this server."""


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    snapshot: str | None = None


def parse_sources(value: str | None) -> tuple[SourceSpec, ...]:
    """Parse ``CLINICAL_TRACE_DATA_SOURCES``. Unset or blank means the synthetic fixture only."""
    if value is None or not value.strip():
        return (SourceSpec(SYNTHETIC),)
    specs: list[SourceSpec] = []
    for entry in value.split(","):
        source_id, separator, target = (part.strip() for part in entry.partition("="))
        if not source_id:
            raise SourceConfigError(f"{ENV_VAR} has an empty entry")
        if any(spec.source_id == source_id for spec in specs):
            raise SourceConfigError(f"{ENV_VAR} lists data source '{source_id}' twice")
        if source_id == SYNTHETIC:
            if separator:
                raise SourceConfigError("the synthetic data source takes no snapshot")
        elif _EPIC_SOURCE_ID.fullmatch(source_id):
            if not target:
                raise SourceConfigError(f"data source '{source_id}' needs a snapshot id or path")
        else:
            raise SourceConfigError(
                f"{ENV_VAR} names an unknown data source; use '{SYNTHETIC}' or 'epic-sandbox[-<suffix>]'"
            )
        specs.append(SourceSpec(source_id, target or None))
    if specs[0].source_id != SYNTHETIC:
        raise SourceConfigError(
            f"'{SYNTHETIC}' must be the first data source, so requests without a source keep their behaviour"
        )
    return tuple(specs)


def tracing_enabled() -> bool:
    """True when LangSmith tracing is switched on (environment, or LangSmith's own context/global settings)."""
    if any(os.getenv(name, "").strip().lower() == "true" for name in _TRACING_ENV):
        return True
    try:
        from langsmith import utils as ls_utils
    except ImportError:  # pragma: no cover - no LangSmith client installed, so nothing can upload traces
        return False
    return bool(ls_utils.tracing_is_enabled())


def source_provenance(resource: Mapping[str, Any]) -> dict[str, Any] | None:
    """The Epic lineage stored inside a mapped resource, or None when it has none (synthetic records)."""
    for extension in resource.get("extension") or []:
        if not isinstance(extension, dict) or extension.get("url") != EPIC_PROVENANCE_URL:
            continue
        lineage: dict[str, Any] = {key: None for key in _PROVENANCE_FIELDS.values()}
        statuses: dict[str, list[str]] = {status: [] for status in _FIELD_STATUSES}
        for part in extension.get("extension") or []:
            url = part.get("url")
            value = next((v for k, v in part.items() if k.startswith("value")), None)
            if url in _PROVENANCE_FIELDS:
                lineage[_PROVENANCE_FIELDS[url]] = value
            elif url in statuses and isinstance(value, str):
                statuses[url].append(value)
        meta = resource.get("meta") or {}
        return {
            "source_url": meta.get("source"),
            **lineage,
            "retrieved_at_note": RETRIEVED_AT_NOTE,
            "field_status": statuses,
        }
    return None


@dataclass
class DataSource:
    """One configured source and the runtime that serves it."""

    source_id: str
    kind: str
    runtime: Runtime
    origin: str
    manifest: Mapping[str, Any] | None = None
    personas: Mapping[str, Persona] | None = None  # None: the application's synthetic personas

    @property
    def is_synthetic(self) -> bool:
        return self.kind == SYNTHETIC_KIND

    def persona(self, persona_id: str | None) -> Persona | None:
        if self.personas is None:
            return resolve_persona(persona_id)
        return self.personas.get(persona_id.strip()) if persona_id else None

    def persona_list(self) -> list[Persona]:
        return list((PERSONAS if self.personas is None else self.personas).values())

    def panel_patients(self) -> tuple[str, ...]:
        """Patients the policy matrix is evaluated against."""
        if self.manifest is None:
            return ALL_SYNTHETIC_PATIENTS
        return tuple(p["internal_id"] for p in self.manifest["patients"])

    def snapshot_summary(self) -> dict[str, Any] | None:
        if self.manifest is None:
            return None
        return {
            key: self.manifest.get(key) for key in ("snapshot_id", "mode", "retrieved_at", "source", "mapping_version")
        }

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "id": self.source_id,
            "kind": self.kind,
            "origin": self.origin,
            "patients": len(self.runtime.store.patient_ids()),
            "inference": "llm" if self.runtime.provider else "deterministic",
            "readmission_model": self.runtime.model_available,
        }
        if self.manifest is not None:
            info["snapshot"] = self.snapshot_summary()
        return info

    def coverage(self, patient_id: str) -> dict[str, Any]:
        """What this source declares it imported (counts, statuses and scope notes; no record content)."""
        base = {"patient_id": patient_id, "source": self.source_id, "kind": self.kind}
        if self.manifest is None:
            return {**base, "declared": False, "note": SYNTHETIC_COVERAGE_NOTE}
        from src.interop.epic.workspace import medication_coverage

        manifest = self.manifest
        fetch = manifest.get("fetch") or {}
        mapping = manifest.get("mapping") or {}
        notes = fetch.get("notes") or {}
        return {
            **base,
            "declared": True,
            "snapshot": self.snapshot_summary(),
            "resource_counts": dict(manifest.get("resource_counts") or {}),
            "structured_data": medication_coverage(manifest),
            "mapping": {
                key: mapping.get(key)
                for key in ("received", "mapped", "rejected", "skipped_types", "field_status", "dangling_references")
            },
            "notes": {
                "fetched": notes.get("fetched"),
                "skipped_note_cap": notes.get("skipped_note_cap"),
                "selection": fetch.get("note_selection"),
            },
            "operations": [
                {"operation": op.get("operation"), "status": op.get("status")} for op in fetch.get("operations") or []
            ],
            "scope_notes": list(manifest.get("notes") or []),
            "retrieved_at_note": RETRIEVED_AT_NOTE,
        }


class SourceRegistry:
    """The configured data sources. The first one (always ``synthetic``) serves requests without a source."""

    def __init__(self, sources: list[DataSource]) -> None:
        if not sources or sources[0].source_id != SYNTHETIC or not sources[0].is_synthetic:
            raise SourceConfigError(f"'{SYNTHETIC}' must be the first data source")
        self._sources = {source.source_id: source for source in sources}
        if len(self._sources) != len(sources):
            raise SourceConfigError("data source ids must be unique")
        self.default = sources[0]

    @classmethod
    def single(cls, runtime: Runtime) -> SourceRegistry:
        """The default configuration: one runtime serving the synthetic fixture."""
        return cls([synthetic_source(runtime)])

    @property
    def multi(self) -> bool:
        return len(self._sources) > 1

    def get(self, source_id: str | None) -> DataSource:
        if source_id is None:
            return self.default
        if not _SOURCE_ID.fullmatch(source_id):
            raise DataSourceError("the X-Data-Source header is malformed")
        try:
            return self._sources[source_id]
        except KeyError:
            raise DataSourceError("the requested data source is not configured on this server") from None

    def sources(self) -> list[DataSource]:
        return list(self._sources.values())

    def describe(self) -> list[dict[str, Any]]:
        return [{**source.describe(), "default": source is self.default} for source in self._sources.values()]

    def close(self, grace_s: float = 10.0) -> bool:
        closed = True
        for source in reversed(self.sources()):
            closed = source.runtime.close(grace_s) and closed
        return closed


def synthetic_source(runtime: Runtime) -> DataSource:
    return DataSource(SYNTHETIC, SYNTHETIC_KIND, runtime, SYNTHETIC_ORIGIN)


def _source_personas(source_id: str, patient_ids: tuple[str, ...]) -> dict[str, Persona]:
    """Same persona ids and roles; a full synthetic panel becomes this source's patients, any other panel none."""
    personas: dict[str, Persona] = {}
    for persona_id, base in PERSONAS.items():
        if base.role == "admin":
            personas[persona_id] = replace(base, panel=())
        elif set(base.panel) == set(ALL_SYNTHETIC_PATIENTS):
            personas[persona_id] = replace(
                base,
                panel=patient_ids,
                description=(f"{base.display_name} with access to every patient in the '{source_id}' data source."),
            )
        else:
            personas[persona_id] = replace(
                base,
                panel=(),
                description=(
                    f"{base.display_name} with no patients in the '{source_id}' data source (access-control demo)."
                ),
            )
    return personas


def _epic_origin(manifest: Mapping[str, Any]) -> str:
    snapshot_id = manifest.get("snapshot_id")
    if manifest.get("mode") == "LIVE":
        return (
            f"Epic on FHIR public sandbox test records, snapshot {snapshot_id} retrieved "
            f"{manifest.get('retrieved_at')} (no real patients)"
        )
    return (
        f"Epic-shaped MOCK fixture from the project's tests, snapshot {snapshot_id} (not Epic data; no real patients)"
    )


def _epic_source(spec: SourceSpec) -> DataSource:
    from src.interop.epic import workspace
    from src.runtime import build_runtime

    try:
        snapshot = workspace.open_snapshot(spec.snapshot or "")
    except (workspace.WorkspaceError, OSError) as exc:
        raise SourceConfigError(f"data source '{spec.source_id}': {exc}") from None
    if snapshot.manifest.get("mode") not in ("LIVE", "MOCK"):
        raise SourceConfigError(f"data source '{spec.source_id}': the snapshot manifest has no MOCK/LIVE mode")
    # In memory: serving a snapshot never writes into its directory, and nothing outlives the process.
    runtime = build_runtime(":memory:", fixture=snapshot.bundle_path, provider=None, enable_model=False)
    try:
        runtime.records.source_coverage = workspace.medication_coverage(snapshot.manifest)
        if sorted(runtime.store.patient_ids()) != sorted(snapshot.patient_ids):
            raise SourceConfigError(f"data source '{spec.source_id}': the snapshot holds unexpected patients")
    except BaseException:
        runtime.close()
        raise
    return DataSource(
        spec.source_id,
        EPIC_KIND,
        runtime,
        _epic_origin(snapshot.manifest),
        snapshot.manifest,
        _source_personas(spec.source_id, snapshot.patient_ids),
    )


def _require_disjoint_patients(sources: list[DataSource]) -> None:
    owner: dict[str, str] = {}
    for source in sources:
        for patient_id in source.runtime.store.patient_ids():
            if patient_id in owner:
                raise SourceConfigError(
                    f"a patient id appears in data sources '{owner[patient_id]}' and '{source.source_id}'"
                )
            owner[patient_id] = source.source_id


def build_registry(
    value: str | None = None, *, db_path: Any = None, provider: Any = "env", enable_model: bool = True
) -> SourceRegistry:
    """Build every configured source. ``value`` defaults to ``CLINICAL_TRACE_DATA_SOURCES``.

    With the variable unset, this builds exactly the runtime the application always built.
    """
    from src.runtime import build_runtime

    specs = parse_sources(os.getenv(ENV_VAR) if value is None else value)
    if len(specs) > 1 and tracing_enabled():
        raise SourceConfigError("LangSmith tracing is enabled; Epic data sources run only with tracing off")
    sources = [synthetic_source(build_runtime(db_path, provider=provider, enable_model=enable_model))]
    try:
        if len(specs) > 1 and sources[0].runtime.provider is not None:
            raise SourceConfigError(
                "a language-model provider is configured; Epic data sources are deterministic-only "
                "(unset CLINICAL_TRACE_LLM_PROVIDER)"
            )
        for spec in specs[1:]:
            sources.append(_epic_source(spec))
        if len(sources) > 1:
            _require_disjoint_patients(sources)
        return SourceRegistry(sources)
    except BaseException:
        for source in sources:
            source.runtime.close()
        raise
