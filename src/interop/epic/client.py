"""Read-only, patient-bound FHIR R4 client pinned to the Epic on FHIR sandbox.

Every URL, including pagination links, must stay on the sandbox host and FHIR base
path; redirects are never followed, so the bearer token cannot be sent elsewhere.
Search results are accepted only when each resource is bound to the requested
patient. Responses are size-capped, retries are bounded, and errors carry only a
fixed category, never a token, URL query or response body.

The adapter design follows the earlier Phase 1 package (host pinning, no redirects,
size caps, loop detection, subject binding) with the fixes recorded in
``docs/EPIC_SANDBOX.md``.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

import httpx

from src.clinical.fhir_client import FHIRClientError, PatientScopeViolation
from src.interop.epic import settings
from src.interop.epic.auth import EpicTokenProvider
from src.interop.epic.telemetry import EpicTelemetry, safe_code

# Patient-scoped searches enabled for this adapter: resource type -> categories that
# mirror the registered Incoming APIs (None = no category parameter).
SEARCH_PLAN: dict[str, tuple[str | None, ...]] = {
    "Encounter": (None,),
    "Condition": ("problem-list-item", "encounter-diagnosis"),
    "Observation": ("laboratory", "vital-signs"),
    "MedicationRequest": (None,),
    "DocumentReference": ("clinical-note",),
    "DiagnosticReport": (None,),
}
CORE_TYPES = ("Encounter", "Condition", "Observation")
_FHIR_ID = re.compile(r"^[A-Za-z0-9.\-]{1,256}$")
_RETRY_STATUSES = frozenset({429, 502, 503, 504})


class EpicFHIRError(FHIRClientError):
    """A sandbox request failed. ``category`` is a fixed code; no URL query, token or body."""

    def __init__(self, category: str) -> None:
        super().__init__(f"Epic sandbox request failed ({category})")
        self.category = category


class EpicPatientScopeError(PatientScopeViolation):
    """The sandbox returned data that is not verifiably bound to the requested patient."""

    def __init__(self) -> None:
        super().__init__("Epic sandbox returned a resource outside the requested patient")
        self.category = "patient_scope"


def valid_fhir_id(value: str) -> str:
    if not isinstance(value, str) or not _FHIR_ID.fullmatch(value):
        raise ValueError("not a valid FHIR resource id")
    return value


def pinned_url(url: str) -> str:
    """Accept only URLs under the single Epic R4 sandbox base path."""
    parts = urlsplit(url)
    path = parts.path
    lowered = path.lower()
    if (
        parts.scheme != "https"
        or parts.hostname != settings.EPIC_HOST
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or not path.startswith(settings.FHIR_BASE_PATH + "/")
        or "/../" in path
        or "/./" in path
        or path.endswith("/..")
        or "\\" in path
        or "%2f" in lowered
        or "%5c" in lowered
        or "%2e" in lowered
    ):
        raise EpicFHIRError("url_outside_sandbox")
    return url


def subject_patient_id(resource: dict[str, Any]) -> str | None:
    """Patient id from ``subject.reference``: relative or absolute on the sandbox base."""
    subject = resource.get("subject")
    reference = subject.get("reference") if isinstance(subject, dict) else None
    if not isinstance(reference, str):
        return None
    for prefix in ("Patient/", f"{settings.FHIR_BASE}/Patient/"):
        if reference.startswith(prefix):
            candidate = reference[len(prefix) :]
            return candidate if _FHIR_ID.fullmatch(candidate) else None
    return None


@dataclass
class SearchResult:
    resource_type: str
    category: str | None
    resources: list[dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    outcomes: list[tuple[str, str]] = field(default_factory=list)  # (severity, code) only

    def summary(self) -> dict[str, Any]:
        return {
            "resource_type": self.resource_type,
            "category": self.category,
            "count": len(self.resources),
            "pages": self.pages,
            "outcome_codes": sorted({f"{s}:{c}" for s, c in self.outcomes}),
        }


@dataclass(frozen=True)
class BinaryContent:
    reference: str
    content_type: str
    data: bytes = field(repr=False)


class EpicSandboxClient:
    """One authenticated, read-only session against the sandbox."""

    def __init__(
        self,
        tokens: EpicTokenProvider,
        *,
        timeout_s: float = 15.0,
        max_pages: int = 10,
        max_resources: int = 500,
        max_response_bytes: int = 2_000_000,
        max_binary_bytes: int = 512 * 1024,
        max_retries: int = 2,
        max_retry_after_s: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        telemetry: EpicTelemetry | None = None,
    ) -> None:
        if not 1 <= max_pages <= 50 or not 1 <= max_resources <= 5000:
            raise ValueError("page and resource limits are out of range")
        self._tokens = tokens
        self.max_pages = max_pages
        self.max_resources = max_resources
        self.max_response_bytes = max_response_bytes
        self.max_binary_bytes = max_binary_bytes
        self.max_retries = max_retries
        self.max_retry_after_s = max_retry_after_s
        self._sleep = sleep
        self.telemetry = telemetry or tokens.telemetry
        self._http = httpx.Client(
            timeout=timeout_s,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            headers={"User-Agent": settings.USER_AGENT},
        )

    def __enter__(self) -> EpicSandboxClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ transport
    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        header = response.headers.get("retry-after") if response is not None else None
        if header and header.strip().isdigit():
            return min(float(header.strip()), self.max_retry_after_s)
        return min(0.5 * (2**attempt), self.max_retry_after_s)

    def _get(self, url: str, operation: str, resource_type: str, accept: str, limit: int) -> tuple[str, bytes]:
        """GET a pinned URL with bounded retries and one token refresh; returns (content type, body)."""
        checked = pinned_url(url)
        refreshed = False
        attempt = 0
        with self.telemetry.operation(operation, resource_type) as op:
            while True:
                token = self._tokens.token()
                headers = {"Accept": accept, "Authorization": f"Bearer {token.value}"}
                response: httpx.Response | None = None
                failure: str | None = None
                try:
                    with self._http.stream("GET", checked, headers=headers) as streamed:
                        response = streamed
                        op.status_code = streamed.status_code
                        if streamed.status_code == 200:
                            body = bytearray()
                            for chunk in streamed.iter_bytes():
                                body.extend(chunk)
                                if len(body) > limit:
                                    op.error_type = "response_too_large"
                                    raise EpicFHIRError("response_too_large")
                            op.ok = True
                            return streamed.headers.get("content-type", ""), bytes(body)
                except httpx.TimeoutException:
                    failure = "timeout"
                except httpx.HTTPError:
                    failure = "network"
                status = response.status_code if response is not None else None
                if status == 401 and not refreshed:
                    refreshed = True
                    self._tokens.invalidate()
                    self.telemetry.count("token.refresh_after_401")
                    continue
                retryable = failure is not None or status in _RETRY_STATUSES
                if retryable and attempt < self.max_retries:
                    self.telemetry.count(f"retry.{failure or status}")
                    self._sleep(self._retry_delay(response, attempt))
                    attempt += 1
                    continue
                category = failure or self._status_category(status)
                op.error_type = category
                raise EpicFHIRError(category)

    @staticmethod
    def _status_category(status: int | None) -> str:
        if status is None:
            return "network"
        if 300 <= status < 400:
            return "redirect_refused"
        return {401: "unauthorized", 403: "forbidden", 404: "not_found", 429: "throttled"}.get(
            status, "http_5xx" if status >= 500 else "http_4xx"
        )

    def _get_json(self, url: str, operation: str, resource_type: str) -> dict[str, Any]:
        content_type, body = self._get(url, operation, resource_type, "application/fhir+json", self.max_response_bytes)
        if "json" not in content_type.lower():
            raise EpicFHIRError("unexpected_content_type")
        try:
            document = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise EpicFHIRError("invalid_json") from None
        if not isinstance(document, dict):
            raise EpicFHIRError("invalid_json")
        return document

    # ------------------------------------------------------------------ reads
    def read_patient(self, patient_id: str) -> dict[str, Any]:
        patient_id = valid_fhir_id(patient_id)
        resource = self._get_json(f"{settings.FHIR_BASE}/Patient/{patient_id}", "read", "Patient")
        if resource.get("resourceType") != "Patient" or resource.get("id") != patient_id:
            raise EpicPatientScopeError()
        return resource

    def search(self, resource_type: str, patient_id: str, *, category: str | None = None) -> SearchResult:
        """Patient-scoped search following pinned ``next`` links; fails closed on any scope doubt."""
        patient_id = valid_fhir_id(patient_id)
        if resource_type not in SEARCH_PLAN or category not in SEARCH_PLAN[resource_type]:
            raise ValueError("resource type or category is not enabled for this adapter")
        params = {"patient": patient_id, **({"category": category} if category else {})}
        result = SearchResult(resource_type=resource_type, category=category)
        url: str | None = f"{settings.FHIR_BASE}/{resource_type}?{urlencode(params)}"
        visited: set[str] = set()
        while url is not None:
            if url in visited:
                raise EpicFHIRError("pagination_loop")
            if len(visited) >= self.max_pages:
                raise EpicFHIRError("page_limit")
            visited.add(url)
            bundle = self._get_json(url, "search", resource_type)
            result.pages += 1
            if bundle.get("resourceType") != "Bundle" or bundle.get("type") != "searchset":
                raise EpicFHIRError("not_a_searchset")
            entries = bundle.get("entry") or []
            if not isinstance(entries, list):
                raise EpicFHIRError("malformed_bundle")
            for entry in entries:
                self._accept_entry(entry, resource_type, patient_id, result)
            if len(result.resources) > self.max_resources:
                raise EpicFHIRError("resource_limit")
            url = self._next_link(bundle)
        self.telemetry.count(f"pages.{resource_type}", result.pages)
        self.telemetry.count(f"resources.received.{resource_type}", len(result.resources))
        return result

    def _accept_entry(self, entry: Any, resource_type: str, patient_id: str, result: SearchResult) -> None:
        if not isinstance(entry, dict) or not isinstance(entry.get("resource"), dict):
            raise EpicFHIRError("malformed_bundle")
        resource = entry["resource"]
        search = entry.get("search")
        mode = search.get("mode") if isinstance(search, dict) else None
        if mode == "outcome" or resource.get("resourceType") == "OperationOutcome":
            for issue in resource.get("issue") or []:
                if isinstance(issue, dict):
                    codes = [
                        c.get("code") for c in (issue.get("details") or {}).get("coding") or [] if isinstance(c, dict)
                    ]
                    pair = (safe_code(issue.get("severity")), safe_code(codes[0] if codes else issue.get("code")))
                    result.outcomes.append(pair)
                    self.telemetry.count(f"outcome.{pair[0]}.{pair[1]}")
            return
        if mode == "include":
            self.telemetry.count(f"include_skipped.{resource_type}")
            return
        if resource.get("resourceType") != resource_type:
            raise EpicFHIRError("unexpected_resource_type")
        if subject_patient_id(resource) != patient_id:
            self.telemetry.count("patient_scope_rejection")
            raise EpicPatientScopeError()
        if not isinstance(resource.get("id"), str) or not _FHIR_ID.fullmatch(resource["id"]):
            raise EpicFHIRError("invalid_resource_id")
        result.resources.append(resource)

    @staticmethod
    def _next_link(bundle: dict[str, Any]) -> str | None:
        links = bundle.get("link") or []
        if not isinstance(links, list):
            raise EpicFHIRError("malformed_bundle")
        targets = [link.get("url") for link in links if isinstance(link, dict) and link.get("relation") == "next"]
        if len(targets) > 1:
            raise EpicFHIRError("ambiguous_next_link")
        if not targets:
            return None
        if not isinstance(targets[0], str):
            raise EpicFHIRError("malformed_bundle")
        return pinned_url(targets[0])

    def read_binary(self, reference: str, *, allowed: set[str]) -> BinaryContent:
        """Fetch note content only for a Binary that a verified DocumentReference of this patient cites."""
        relative = (
            reference[len(settings.FHIR_BASE) + 1 :] if reference.startswith(settings.FHIR_BASE + "/") else reference
        )
        if relative not in allowed or not relative.startswith("Binary/"):
            self.telemetry.count("binary.rejected_not_cited")
            raise EpicPatientScopeError()
        binary_id = valid_fhir_id(relative.split("/", 1)[1])
        content_type, body = self._get(
            f"{settings.FHIR_BASE}/Binary/{binary_id}", "read", "Binary", "application/fhir+json", self.max_binary_bytes
        )
        if "json" in content_type.lower():
            try:
                document = json.loads(body)
                data = base64.b64decode(document["data"], validate=True)
                declared = str(document.get("contentType") or "")
            except (ValueError, KeyError, TypeError, binascii.Error):
                raise EpicFHIRError("invalid_binary") from None
            if document.get("resourceType") != "Binary":
                raise EpicFHIRError("invalid_binary")
            if len(data) > self.max_binary_bytes:
                raise EpicFHIRError("response_too_large")
            return BinaryContent(reference=relative, content_type=declared, data=data)
        return BinaryContent(reference=relative, content_type=content_type, data=body)
