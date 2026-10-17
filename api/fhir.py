"""Read-only mock FHIR R4 server over the synthetic store (``/fhir/r4``).

Supports ``metadata``, read, version read, and patient-scoped search with
``_count`` / ``_offset`` paging. Access requires a demo persona and is limited
to that persona's patient panel. This is a local simulation, not a connection
to any real EHR.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from api.v2 import access, runtime
from src.clinical.fhir_client import PATIENT_SCOPED_TYPES, searchset_bundle
from src.clinical.fhir_models import SUPPORTED_TYPES, patient_id_of
from src.runtime import Runtime
from src.safety.policy import AccessContext, Action

router = APIRouter(prefix="/fhir/r4", tags=["mock FHIR R4 (synthetic, read-only)"])
FHIR_JSON = "application/fhir+json"


def _outcome(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"resourceType": "OperationOutcome",
         "issue": [{"severity": "error", "code": code, "diagnostics": message}]},
        status_code=status, media_type=FHIR_JSON,
    )


def _fhir(payload: dict[str, Any]) -> JSONResponse:
    return JSONResponse(payload, media_type=FHIR_JSON)


@router.get("/metadata")
def capability_statement() -> JSONResponse:
    return _fhir({
        "resourceType": "CapabilityStatement",
        "status": "active",
        "kind": "instance",
        "fhirVersion": "4.0.1",
        "format": ["json"],
        "software": {"name": "Clinical-Trace AI mock FHIR server (synthetic data only)"},
        "rest": [{
            "mode": "server",
            "resource": [
                {"type": t, "interaction": [{"code": "read"}, {"code": "vread"}]
                 + ([{"code": "search-type"}] if t in PATIENT_SCOPED_TYPES else []),
                 "searchParam": [{"name": "patient", "type": "reference"}] if t in PATIENT_SCOPED_TYPES else []}
                for t in SUPPORTED_TYPES
            ],
        }],
    })


def _authorized_resource(rt: Runtime, ctx: AccessContext, resource: dict[str, Any] | None) -> JSONResponse | None:
    if resource is None:
        return _outcome(404, "not-found", "resource not found")
    patient = patient_id_of(resource)
    if not rt.policy.authorize(ctx, Action.PATIENT_READ, patient).allowed:
        # Same response as a missing resource: do not reveal that it exists.
        return _outcome(404, "not-found", "resource not found")
    return None


@router.get("/{resource_type}/{resource_id}")
def read(resource_type: str, resource_id: str, ctx: AccessContext = Depends(access),
         rt: Runtime = Depends(runtime)) -> JSONResponse:
    if resource_type not in SUPPORTED_TYPES:
        return _outcome(400, "not-supported", f"resource type {resource_type} is not supported")
    resource = rt.store.get(resource_type, resource_id)
    denied = _authorized_resource(rt, ctx, resource)
    return denied or _fhir(resource)  # type: ignore[arg-type]


@router.get("/{resource_type}/{resource_id}/_history/{version_id}")
def vread(resource_type: str, resource_id: str, version_id: int, ctx: AccessContext = Depends(access),
          rt: Runtime = Depends(runtime)) -> JSONResponse:
    if resource_type not in SUPPORTED_TYPES:
        return _outcome(400, "not-supported", f"resource type {resource_type} is not supported")
    resource = rt.store.get(resource_type, resource_id, version=version_id)
    denied = _authorized_resource(rt, ctx, resource)
    return denied or _fhir(resource)  # type: ignore[arg-type]


@router.get("/{resource_type}")
def search(resource_type: str, request: Request, patient: str = Query(..., max_length=64),
           count: int = Query(20, alias="_count", ge=1, le=100), offset: int = Query(0, alias="_offset", ge=0),
           ctx: AccessContext = Depends(access), rt: Runtime = Depends(runtime)) -> JSONResponse:
    if resource_type not in PATIENT_SCOPED_TYPES:
        return _outcome(400, "not-supported", "search requires a patient-scoped resource type")
    patient_id = patient.split("/", 1)[1] if patient.startswith("Patient/") else patient
    if not rt.policy.authorize(ctx, Action.PATIENT_READ, patient_id).allowed:
        return _outcome(403, "forbidden", "patient is outside the persona's panel")
    base = str(request.base_url).rstrip("/") + "/fhir/r4"
    return _fhir(searchset_bundle(rt.store, base, resource_type, patient_id, count, offset))
