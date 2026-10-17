"""Read-only FHIR R4 client adapter and searchset bundle builder.

``FHIRHttpClient`` talks to any FHIR R4 server exposing ``[base]/{type}/{id}``
and ``[base]/{type}?patient=...`` - including the local mock server mounted at
``/fhir/r4`` by the API. It enforces timeouts, bounded retries with backoff,
pagination limits, schema validation, and patient scoping (a resource for a
different patient in a patient-scoped search is a hard error).

``pull_patient`` + ``FHIRStore.ingest`` form the incremental ingestion path:
external FHIR -> validation -> idempotent versioned store.

This is not an authorised connection to any real EHR.
"""

from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from src.clinical.fhir_models import SUPPORTED_TYPES, patient_id_of, validate_resource
from src.clinical.store import FHIRStore

PATIENT_SCOPED_TYPES = tuple(t for t in SUPPORTED_TYPES if t != "Patient")


class FHIRClientError(RuntimeError):
    pass


class PatientScopeViolation(FHIRClientError):
    pass


def searchset_bundle(
    store: FHIRStore,
    base_url: str,
    resource_type: str,
    patient_id: str,
    count: int,
    offset: int,
) -> dict[str, Any]:
    """Build a paginated FHIR ``searchset`` Bundle from the local store."""
    page, total = store.search(patient_id, resource_type, offset=offset, limit=count)

    def link(rel: str, off: int) -> dict[str, str]:
        query = urlencode({"patient": patient_id, "_count": count, "_offset": off})
        return {"relation": rel, "url": f"{base_url}/{resource_type}?{query}"}

    links = [link("self", offset)]
    if offset + count < total:
        links.append(link("next", offset + count))
    if offset > 0:
        links.append(link("previous", max(offset - count, 0)))
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": total,
        "link": links,
        "entry": [
            {"fullUrl": f"{base_url}/{r['resourceType']}/{r['id']}", "resource": r,
             "search": {"mode": "match"}}
            for r in page
        ],
    }


class FHIRHttpClient:
    def __init__(
        self,
        base_url: str,
        timeout_s: float = 5.0,
        max_retries: int = 2,
        backoff_s: float = 0.2,
        max_pages: int = 20,
        headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.max_pages = max_pages
        self._sleep = sleep
        self._client = httpx.Client(
            timeout=timeout_s,
            headers={"Accept": "application/fhir+json", **(headers or {})},
            transport=transport,
        )

    def _get(self, url: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.get(url)
            except httpx.TimeoutException as exc:
                last_error = exc
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if response.status_code < 500:
                    if response.status_code == 404:
                        raise FHIRClientError(f"not found: {url}")
                    if response.status_code >= 400:
                        raise FHIRClientError(f"request rejected with HTTP {response.status_code}")
                    return response.json()
                last_error = FHIRClientError(f"server error HTTP {response.status_code}")
            if attempt < self.max_retries:
                self._sleep(self.backoff_s * (2 ** attempt))
        raise FHIRClientError(f"FHIR request failed after {self.max_retries + 1} attempts: {last_error}")

    def read(self, resource_type: str, resource_id: str) -> dict[str, Any]:
        resource = self._get(f"{self.base_url}/{resource_type}/{resource_id}")
        issues = validate_resource(resource)
        if issues:
            raise FHIRClientError(f"invalid {resource_type}/{resource_id}: {issues}")
        return resource

    def search(self, resource_type: str, patient_id: str, count: int = 50) -> list[dict[str, Any]]:
        """Patient-scoped search following ``next`` links up to ``max_pages``."""
        if resource_type not in PATIENT_SCOPED_TYPES:
            raise FHIRClientError(f"unsupported patient-scoped type {resource_type}")
        url: str | None = f"{self.base_url}/{resource_type}?{urlencode({'patient': patient_id, '_count': count})}"
        resources: list[dict[str, Any]] = []
        pages = 0
        while url:
            pages += 1
            if pages > self.max_pages:
                raise FHIRClientError("pagination limit exceeded")
            bundle = self._get(url)
            for entry in bundle.get("entry") or []:
                resource = entry.get("resource") or {}
                if patient_id_of(resource) != patient_id:
                    raise PatientScopeViolation(
                        f"server returned {resource.get('resourceType')}/{resource.get('id')} "
                        "for a different patient"
                    )
                issues = validate_resource(resource)
                if issues:
                    raise FHIRClientError(f"invalid resource in searchset: {issues}")
                resources.append(resource)
            url = next(
                (item["url"] for item in bundle.get("link") or [] if item.get("relation") == "next"), None
            )
        return resources

    def pull_patient(self, patient_id: str) -> list[dict[str, Any]]:
        resources = [self.read("Patient", patient_id)]
        for resource_type in PATIENT_SCOPED_TYPES:
            resources.extend(self.search(resource_type, patient_id))
        return resources

    def close(self) -> None:
        self._client.close()
