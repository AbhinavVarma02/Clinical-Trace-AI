"""Strict, bounded acquisition of the initial Epic R4 profile via in-process MOCK only."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa

from src.interop.epic import settings
from src.interop.epic.auth import (
    CLIENT_ASSERTION_TYPE,
    MAX_TOKEN_RESPONSE_BYTES,
    AccessToken,
    build_client_assertion,
    parse_access_token,
)
from src.interop.epic.beta_catalog import Catalog, RefreshRejected
from src.interop.epic.client import (
    EpicFHIRError,
    EpicSandboxClient,
    pinned_url,
    subject_patient_id,
    valid_fhir_id,
)

PROFILE = (
    ("Encounter", None),
    ("Condition", "problem-list-item"),
    ("Condition", "encounter-diagnosis"),
    ("Observation", "laboratory"),
    ("Observation", "vital-signs"),
    ("DocumentReference", "clinical-note"),
    ("DiagnosticReport", None),
)
CATEGORY_SYSTEM = {
    "Condition": "http://terminology.hl7.org/CodeSystem/condition-category",
    "Observation": "http://terminology.hl7.org/CodeSystem/observation-category",
    "DocumentReference": "http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category",
}
MOCK_CLIENT = "invented-backend-client"
MOCK_SCOPES = frozenset(f"system/{kind}.read" for kind in ("Patient", *(kind for kind, _ in PROFILE)))
CURSOR = re.compile(r"[A-Za-z0-9_-]{1,128}")
BEARER_VALUE = re.compile(r"[A-Za-z0-9._~+/-]+=*")
RETRY_STATUSES = frozenset({429, 502, 503, 504})


@dataclass(frozen=True)
class Limits:
    cooldown_s: int = 300
    patient_per_hour: int = 2
    global_per_hour: int = 6
    global_requests_per_hour: int = 240
    requests_per_attempt: int = 48
    deadline_s: float = 90
    per_request_s: float = 5
    retry_delay_s: float = 0.5
    pages_per_search: int = 4
    matches_per_search: int = 200
    matches_per_attempt: int = 1200
    response_bytes: int = 2_000_000
    attempt_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        for name, field in self.__dataclass_fields__.items():
            value = getattr(self, name)
            if not math.isfinite(value) or value > field.default or value < 0:
                raise ValueError("MOCK budget overrides may only tighten defaults")
            if name not in {"cooldown_s", "retry_delay_s"} and value == 0:
                raise ValueError("MOCK budget must be positive")


class AcquisitionError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def require_mock(transport: httpx.MockTransport) -> None:
    if type(transport) is not httpx.MockTransport:
        raise TypeError("an in-process httpx.MockTransport is required")


def object_json(content: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError
        return result

    def invalid_constant(_: str) -> None:
        raise ValueError

    try:
        body = json.loads(content.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
    except (ValueError, RecursionError):
        raise AcquisitionError("invalid_json") from None
    if not isinstance(body, dict):
        raise AcquisitionError("invalid_json")
    return body


async def mock_response(
    transport: httpx.MockTransport,
    request: httpx.Request,
    *,
    seconds: float,
    limit: int,
    consume: Callable[[int], None] | None = None,
) -> tuple[int, httpx.Headers, bytes]:
    """One bounded call without httpx client URL logging, proxies, redirects or DNS."""
    try:
        async with asyncio.timeout(seconds):
            response = await transport.handle_async_request(request)
            try:
                status = response.status_code
                if status != 200:
                    return status, response.headers, b""
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise AcquisitionError("unsupported_encoding")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if consume is not None:
                        consume(len(chunk))
                    if len(body) + len(chunk) > limit:
                        raise AcquisitionError("response_too_large")
                    body.extend(chunk)
                return status, response.headers, bytes(body)
            finally:
                await response.aclose()
    except (TimeoutError, httpx.TimeoutException):
        raise AcquisitionError("timeout") from None
    except httpx.HTTPError:
        raise AcquisitionError("network") from None


def continuation(url: str, initial: str) -> str:
    """Allow a MOCK cursor on exactly the same search, with canonical query encoding."""
    try:
        pinned_url(url)
        base, query = initial.split("?")
        prefix = f"{base}?{query}&_cursor="
        if not url.startswith(prefix) or not CURSOR.fullmatch(url[len(prefix) :]):
            raise ValueError
    except (EpicFHIRError, ValueError):
        raise AcquisitionError("pagination_scope") from None
    return url


def _matches_category(resource: dict[str, Any], category: str, system: str) -> bool:
    concepts = resource.get("category")
    if not isinstance(concepts, list):
        return False
    for concept in concepts:
        codings = concept.get("coding") if isinstance(concept, dict) else None
        if isinstance(codings, list) and any(
            isinstance(coding, dict) and coding.get("code") == category and coding.get("system") == system
            for coding in codings
        ):
            return True
    return False


class Acquisition:
    def __init__(
        self,
        catalog: Catalog,
        attempt: str,
        patient: str,
        key: rsa.RSAPrivateKey,
        transport: httpx.MockTransport,
        limits: Limits,
        deadline: float,
        authorized: Callable[[], bool],
        *,
        retain: bool = False,
    ) -> None:
        self.catalog, self.attempt, self.patient, self.key = catalog, attempt, patient, key
        self.transport, self.limits, self.deadline, self.authorized = transport, limits, deadline, authorized
        self.bytes = 0
        self.matches = 0
        self.token: AccessToken | None = None
        self.renewed = False
        self.seen: dict[tuple[str, str], bytes] = {}
        self.resources: dict[tuple[str, str], dict[str, Any]] | None = {} if retain else None
        self.resource_operations: dict[tuple[str, str], set[str]] = {}
        self.patient_url = f"{settings.FHIR_BASE}/Patient/{valid_fhir_id(patient)}"
        self.search_urls = {
            (kind, category): f"{settings.FHIR_BASE}/{kind}?"
            + urlencode({"patient": patient, **({"category": category} if category else {})})
            for kind, category in PROFILE
        }
        self.operations: dict[str, dict[str, Any]] = {
            (f"{kind}:{category}" if category else kind): {
                "execution": "not_requested",
                "matches": 0,
                "pages": 0,
                "unique": 0,
            }
            for kind, category in (("Patient", None), *PROFILE)
        }
        if retain:
            for counts in self.operations.values():
                counts.update(imported=0, excluded=0, rejected=0, duplicates=0, **{"import": "not_imported"})

    def checkpoint(self) -> float:
        if not self.authorized():
            raise AcquisitionError("authorization_revoked")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AcquisitionError("deadline")
        return remaining

    def consume(self, size: int) -> None:
        self.checkpoint()
        self.bytes += size
        if self.bytes > self.limits.attempt_bytes:
            raise AcquisitionError("byte_limit")

    async def run(self) -> None:
        await self.operation("Patient", None)
        for kind, category in PROFILE:
            await self.operation(kind, category)
        self.checkpoint()

    async def operation(self, kind: str, category: str | None) -> None:
        name = f"{kind}:{category}" if category else kind
        counts = self.operations[name]
        try:
            if kind == "Patient":
                resource = await self.fhir(self.patient_url)
                if resource.get("resourceType") != "Patient" or resource.get("id") != self.patient:
                    raise AcquisitionError("patient_scope")
                self.retain_resource(resource, name)
                counts.update(matches=1, unique=1, pages=1)
            else:
                await self.search(kind, category, name, counts)
        except (AcquisitionError, RefreshRejected, asyncio.CancelledError) as exc:
            counts["execution"] = (
                "truncated"
                if getattr(exc, "code", None)
                in {"byte_limit", "deadline", "request_limit", "outbound_quota", "page_limit", "resource_limit"}
                else "failed"
            )
            raise
        counts["execution"] = "succeeded"

    async def search(self, kind: str, category: str | None, name: str, counts: dict[str, Any]) -> None:
        initial = self.search_urls[kind, category]
        url = initial
        visited: set[str] = set()
        operation_ids: set[str] = set()
        expected: int | None = None
        while True:
            if url in visited:
                raise AcquisitionError("pagination_loop")
            if len(visited) >= self.limits.pages_per_search:
                raise AcquisitionError("page_limit")
            visited.add(url)
            bundle = await self.fhir(url)
            counts["pages"] += 1
            if bundle.get("resourceType") != "Bundle" or bundle.get("type") != "searchset":
                raise AcquisitionError("invalid_bundle")
            entries, links = bundle.get("entry", []), bundle.get("link", [])
            if not isinstance(entries, list) or not isinstance(links, list):
                raise AcquisitionError("invalid_bundle")
            if "total" in bundle:
                total = bundle["total"]
                if type(total) is not int or total < 0 or (expected is not None and total != expected):
                    raise AcquisitionError("invalid_bundle")
                expected = total
            for entry in entries:
                self.checkpoint()
                if (
                    counts["matches"] >= self.limits.matches_per_search
                    or self.matches >= self.limits.matches_per_attempt
                ):
                    raise AcquisitionError("resource_limit")
                rid = self.accept(entry, kind, category, name)
                self.matches += 1
                counts["matches"] += 1
                operation_ids.add(rid)
                counts["unique"] = len(operation_ids)
            targets = []
            for link in links:
                if not isinstance(link, dict) or not isinstance(link.get("relation"), str):
                    raise AcquisitionError("invalid_bundle")
                if link["relation"] == "next":
                    targets.append(link.get("url"))
            if not targets:
                if expected is not None and counts["matches"] != expected:
                    raise AcquisitionError("invalid_bundle")
                return
            if len(targets) != 1 or not isinstance(targets[0], str):
                raise AcquisitionError("pagination_scope")
            url = continuation(targets[0], initial)

    def accept(self, entry: Any, kind: str, category: str | None, name: str) -> str:
        if not isinstance(entry, dict) or not isinstance(entry.get("resource"), dict):
            raise AcquisitionError("invalid_bundle")
        resource = entry["resource"]
        search = entry.get("search", {})
        if not isinstance(search, dict) or search.get("mode", "match") != "match":
            raise AcquisitionError("invalid_bundle")
        if resource.get("resourceType") == "OperationOutcome":
            raise AcquisitionError("invalid_bundle")
        if resource.get("resourceType") != kind:
            raise AcquisitionError("resource_scope")
        if subject_patient_id(resource) != self.patient:
            raise AcquisitionError("patient_scope")
        if category is not None and not _matches_category(resource, category, CATEGORY_SYSTEM[kind]):
            raise AcquisitionError("resource_scope")
        try:
            rid = valid_fhir_id(resource.get("id"))
        except ValueError:
            raise AcquisitionError("resource_scope") from None
        if rid in {".", ".."}:
            raise AcquisitionError("resource_scope")
        if "fullUrl" in entry and entry["fullUrl"] not in (f"{kind}/{rid}", f"{settings.FHIR_BASE}/{kind}/{rid}"):
            raise AcquisitionError("resource_scope")
        try:
            digest = hashlib.sha256(json.dumps(resource, sort_keys=True, allow_nan=False).encode()).digest()
        except (ValueError, RecursionError):
            raise AcquisitionError("invalid_json") from None
        if self.seen.setdefault((kind, rid), digest) != digest:
            raise AcquisitionError("conflicting_identity")
        self.retain_resource(resource, name)
        return rid

    def retain_resource(self, resource: dict[str, Any], name: str) -> None:
        if self.resources is None:
            return
        kind, rid = resource["resourceType"], resource["id"]
        if kind == "DocumentReference":
            from src.interop.epic.mapping import content_sha256

            # Note bodies, inline data, attachment URLs and narrative are never staged.
            metadata = {
                key: resource[key]
                for key in ("resourceType", "id", "meta", "subject", "status", "type", "category", "date", "context")
                if key in resource
            }
            metadata["_sourceContentSha256"] = content_sha256(resource)
            self.resources[kind, rid] = metadata
        else:
            self.resources[kind, rid] = resource
        self.resource_operations.setdefault((kind, rid), set()).add(name)

    async def fhir(self, url: str) -> dict[str, Any]:
        status, headers, body = await self.request("GET", url)
        if status != 200:
            raise AcquisitionError(EpicSandboxClient._status_category(status))
        if headers.get("content-type", "").split(";", 1)[0].strip().lower() not in {
            "application/fhir+json",
            "application/json",
        }:
            raise AcquisitionError("unexpected_content_type")
        return object_json(body)

    async def access_token(self) -> AccessToken:
        now = self.catalog.clock()
        if self.token is not None:
            if self.token.expires_at <= now:
                raise AcquisitionError("token_expired")
            return self.token
        self.checkpoint()
        status, _, body = await self.request("POST", settings.TOKEN_URL)
        if status != 200:
            raise AcquisitionError(EpicSandboxClient._status_category(status))
        try:
            payload = object_json(body)
            if "scope" in payload and not isinstance(payload["scope"], str):
                raise AcquisitionError("malformed_token")
            token = parse_access_token(httpx.Response(200, content=body), self.catalog.clock())
        except (AcquisitionError, OverflowError):
            raise AcquisitionError("malformed_token") from None
        if token is None or not math.isfinite(token.expires_at) or not BEARER_VALUE.fullmatch(token.value):
            raise AcquisitionError("malformed_token")
        if not set(token.scopes) <= MOCK_SCOPES:
            raise AcquisitionError("unapproved_grant")
        self.token = token
        return token

    async def request(self, method: str, url: str) -> tuple[int, httpx.Headers, bytes]:
        if method == "GET":
            initial, separator, _ = url.partition("&_cursor=")
            if url != self.patient_url and initial not in self.search_urls.values():
                raise AcquisitionError("operation_scope")
            if separator:
                continuation(url, initial)
        elif (method, url) != ("POST", settings.TOKEN_URL):
            raise AcquisitionError("operation_scope")
        headers = {"Accept-Encoding": "identity", "User-Agent": settings.USER_AGENT}
        retry_available = True
        while True:
            self.checkpoint()
            if method == "POST":
                assertion = build_client_assertion(self.key, MOCK_CLIENT, now=self.catalog.clock())
                request = httpx.Request(
                    "POST",
                    settings.TOKEN_URL,
                    headers=headers,
                    data={
                        "grant_type": "client_credentials",
                        "client_assertion_type": CLIENT_ASSERTION_TYPE,
                        "client_assertion": assertion,
                    },
                )
            else:
                token = await self.access_token()
                headers.update(Accept="application/fhir+json", Authorization=f"Bearer {token.value}")
                request = httpx.Request("GET", url, headers=headers)
            self.catalog.charge(self.attempt, self.limits.requests_per_attempt, self.limits.global_requests_per_hour)
            remaining = self.checkpoint()
            try:
                result = await mock_response(
                    self.transport,
                    request,
                    seconds=min(remaining, self.limits.per_request_s),
                    limit=MAX_TOKEN_RESPONSE_BYTES if method == "POST" else self.limits.response_bytes,
                    consume=self.consume,
                )
            except AcquisitionError as exc:
                self.checkpoint()
                if exc.code not in {"timeout", "network"} or not retry_available:
                    raise
                retry_available = False
                await self.backoff(None, exc.code)
                continue
            self.checkpoint()
            status, response_headers, _ = result
            if method == "GET" and status == 401 and not self.renewed:
                self.renewed = True
                self.token = None
                continue
            if status not in RETRY_STATUSES or not retry_available:
                return result
            retry_available = False
            await self.backoff(response_headers.get("retry-after"), EpicSandboxClient._status_category(status))

    async def backoff(self, header: str | None, code: str) -> None:
        delay = self.limits.retry_delay_s
        if header:
            try:
                if header.isascii() and header.isdigit():
                    delay = float(header)
                else:
                    delay = max(0, parsedate_to_datetime(header).timestamp() - self.catalog.clock())
            except (ValueError, TypeError, OverflowError, OSError):
                pass
        if delay >= self.checkpoint():
            raise AcquisitionError(code)
        await asyncio.sleep(delay)
