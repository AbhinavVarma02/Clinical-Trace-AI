"""Default-disabled, backend-only Epic sandbox acquisition. No browser or publication route.

Credentials are supplied already unlocked by a future backend caller. This module
never loads configuration, key files, dotenv files or private historical snapshots.
Only the existing eight initial-profile operations can cross the transport boundary.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa

from src.interop.epic import beta_catalog, settings
from src.interop.epic.auth import AccessToken, build_client_assertion
from src.interop.epic.beta_acquisition import (
    CURSOR,
    MOCK_SCOPES,
    Acquisition,
    AcquisitionError,
    Limits,
    bounded_response,
    require_mock,
)
from src.interop.epic.beta_catalog import Catalog, RefreshRejected
from src.interop.epic.client import EpicFHIRError, pinned_url, valid_fhir_id
from src.interop.epic.beta_live_staging import LiveAllowlist
from src.interop.epic.beta_staging import _check_source, encoded, normalize

# Fixed operations, never supplied by a request, environment variable or token grant.
LIVE_OPERATIONS = frozenset(
    {
        "Patient",
        "Encounter",
        "Condition:problem-list-item",
        "Condition:encounter-diagnosis",
        "Observation:laboratory",
        "Observation:vital-signs",
        "DocumentReference:clinical-note",
        "DiagnosticReport",
    }
)
# Existing M1 registration may return these additional read grants. Possession
# never enables a request: LIVE_OPERATIONS and check_request remain the boundary.
LIVE_GRANTS = MOCK_SCOPES | {"system/Binary.read", "system/MedicationRequest.read"}
# Carried in every candidate manifest (and so in its hash) until an authorized attempt resolves the open
# questions: informational code 59100 is unattributed and the shared sandbox returned fewer notes/vitals than
# on 2026-11-06. Clearing this needs a new decision, not a flag.
COMPLETENESS = "unconfirmed"


def sandbox_transport() -> httpx.AsyncHTTPTransport:
    """No ambient proxy/CA overrides, TLS bypass, redirects or transport-level retries."""
    return httpx.AsyncHTTPTransport(
        verify=True,
        trust_env=False,
        retries=0,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    )


@dataclass(frozen=True)
class LiveRefresh:
    """Explicit opt-in for the owner route's LIVE staging path; never constructed by default.

    Supplies an already-unlocked signing key and the existing sandbox configuration. A transport is for
    offline tests only (a ``MockTransport``); ``None`` means the real, TLS-verifying sandbox transport.
    The ``allowlist`` is explicit and immutable (an empty one approves nothing). Even when supplied, the path
    still needs the enable flag and a non-empty allowlist, and it never publishes a generation.
    """

    key: rsa.RSAPrivateKey = field(repr=False)
    config: settings.EpicConfig
    allowlist: LiveAllowlist
    transport: httpx.MockTransport | None = None


class LiveAcquisition(Acquisition):
    policy = beta_catalog.LIVE_POLICY_VERSION
    manifest_version = "epic-acquisition/1"

    def __init__(
        self,
        catalog: Catalog,
        attempt: str,
        alias: str,
        key: rsa.RSAPrivateKey,
        config: settings.EpicConfig,
        *,
        allowlist: LiveAllowlist,
        enabled: bool = False,
        authorized: Callable[[], bool],
        limits: Limits | None = None,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        # All gates precede transport construction, signing or credential use.
        if enabled is not True:
            raise AcquisitionError("live_disabled")
        try:
            settings.require_enabled()
        except settings.SandboxDisabled:
            raise AcquisitionError("live_disabled") from None
        patient = allowlist.get(alias)
        if patient is None or alias in beta_catalog.MOCK_PATIENTS or patient in beta_catalog.MOCK_PATIENTS.values():
            raise AcquisitionError("patient_not_approved")
        try:
            valid_fhir_id(patient)
            if patient in {".", ".."}:
                raise ValueError
        except ValueError:
            raise AcquisitionError("patient_not_approved") from None
        if transport is not None:
            require_mock(transport)
        self.alias, self.config, self.allowlist = alias, config, allowlist
        self.injected_transport = transport is not None  # True only for offline fixtures; recorded in the manifest
        self.mode = "MOCK" if transport is not None else "LIVE"
        self.source = "epic-sandbox-mock" if transport is not None else settings.SOURCE_SYSTEM
        self.candidate: tuple[dict[str, Any], bytes] | None = None
        self.outcome_codes: list[str] = []  # informational search-outcome codes only
        self.operation_codes: dict[str, list[str]] = {}  # the same codes, attributed to the operation
        self._current: str | None = None
        self._ran = False
        limits = limits or Limits()
        super().__init__(
            catalog,
            attempt,
            patient,
            key,
            transport,
            limits,
            time.monotonic() + limits.deadline_s,
            authorized,
            retain=True,
        )
        # Searches send only patient and category, the same shape as the 2026-11-06 M1 client. A request-time
        # `_elements` projection was tried and removed: the first real attempt returned informational code 59100
        # (unsupported parameter), probably from it, though not confirmed. Note metadata is enforced locally:
        # retain_resource projects to metadata and strips any inline body, and no attachment URL is ever followed.
        self.checkpoint()

    def checkpoint(self) -> float:
        remaining = super().checkpoint()
        try:
            settings.require_enabled()
        except settings.SandboxDisabled:
            raise AcquisitionError("live_disabled") from None
        if self.allowlist.get(self.alias) != self.patient:
            raise AcquisitionError("patient_not_approved")
        if self.catalog.revoked("owner") or self.catalog.revoked(self.alias):
            raise AcquisitionError("authorization_revoked")
        row = self.catalog.db.execute(
            "SELECT alias, state, policy FROM attempt WHERE attempt_id = ?", (self.attempt,)
        ).fetchone()
        if row is None or (row["alias"], row["state"], row["policy"]) != (self.alias, "running", self.policy):
            raise AcquisitionError("attempt_scope")
        return remaining

    def client_assertion(self) -> str:
        return build_client_assertion(self.key, self.config.client_id, jku=self.config.jku, now=self.catalog.clock())

    def check_grant(self, token: AccessToken) -> None:
        # Reuse the existing registration without requiring a wider/new grant.
        # Missing required, wildcard, write and unknown grants all fail closed.
        if not MOCK_SCOPES <= set(token.scopes) <= LIVE_GRANTS:
            raise AcquisitionError("unapproved_grant")

    def continuation(self, url: str, initial: str) -> str:
        """Same patient, category and projection; one explicit bounded cursor only.

        Unknown/opaque server sessions without patient filters fail closed. Real
        Epic continuation shapes still need a separately authorized live check.
        """
        try:
            if len(url) > 2048:
                raise ValueError
            pinned_url(url)
            base, initial_query = initial.split("?", 1)
            if not url.startswith(base + "?"):
                raise ValueError
            pairs = parse_qsl(url[len(base) + 1 :], strict_parsing=True, max_num_fields=5)
            query = dict(pairs)
            if len(query) != len(pairs):
                raise ValueError
            required = dict(parse_qsl(initial_query))
            extra = query.keys() - required.keys()
            if len(extra) != 1 or any(query.get(k) != v for k, v in required.items()):
                raise ValueError
            cursor_name = extra.pop()
            cursor = query[cursor_name]
            if cursor_name == "page":
                if not cursor.isascii() or not cursor.isdigit() or not 1 <= int(cursor) <= 1_000_000:
                    raise ValueError
            elif cursor_name != "_cursor" or not CURSOR.fullmatch(cursor):
                raise ValueError
            # Canonical form also collapses alternate encodings/reordered loop links.
            return (
                base + "?" + urlencode({**required, cursor_name: str(int(cursor)) if cursor_name == "page" else cursor})
            )
        except (EpicFHIRError, ValueError, TypeError):
            raise AcquisitionError("pagination_scope") from None

    def check_request(self, method: str, url: str) -> None:
        if (method, url) == ("POST", settings.TOKEN_URL):
            return
        if method != "GET":
            raise AcquisitionError("operation_scope")
        if url == self.patient_url and "Patient" in LIVE_OPERATIONS:
            return
        for (kind, category), initial in self.search_urls.items():
            name = f"{kind}:{category}" if category else kind
            if name not in LIVE_OPERATIONS:
                continue
            if url == initial:
                return
            if url.startswith(initial.split("?", 1)[0] + "?"):
                try:
                    if self.continuation(url, initial) == url:
                        return
                except AcquisitionError:
                    continue
        raise AcquisitionError("operation_scope")

    async def response(self, request: httpx.Request, *, seconds: float, limit: int) -> tuple[int, httpx.Headers, bytes]:
        # Validate again at the last boundary, including each token renewal/retry.
        self.checkpoint()
        self.check_request(request.method, str(request.url))
        assert self.transport is not None
        request.extensions["timeout"] = dict.fromkeys(("connect", "read", "write", "pool"), seconds)
        result = await bounded_response(self.transport, request, seconds=seconds, limit=limit, consume=self.consume)
        status, headers, _ = result
        if request.method == "POST" and status == 200:
            if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise AcquisitionError("malformed_token")
        return result

    @staticmethod
    def _informational_outcome(entry: Any) -> list[str] | None:
        """Codes of an Epic search-outcome entry that carries only warnings/information.

        Epic answers an empty search with such an entry (for example warning 4101),
        which is not a match. Anything else (error/fatal, a non-outcome resource, or
        an outcome presented as a match) returns None and stays subject to the
        strict bundle checks. Only fixed-format codes are retained, never text.
        """
        if not isinstance(entry, dict) or set(entry) - {"fullUrl", "search", "resource"}:
            return None
        search, resource = entry.get("search"), entry.get("resource")
        if not isinstance(search, dict) or search.get("mode") != "outcome" or not isinstance(resource, dict):
            return None
        issues = resource.get("issue")
        if resource.get("resourceType") != "OperationOutcome" or not isinstance(issues, list) or not issues:
            return None
        codes: list[str] = []
        for issue in issues:
            if not isinstance(issue, dict) or issue.get("severity") not in {"information", "warning"}:
                return None
            details = issue.get("details")
            for coding in details.get("coding", []) if isinstance(details, dict) else []:
                code = coding.get("code") if isinstance(coding, dict) else None
                if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,32}", code):
                    codes.append(code)
        return codes

    async def operation(self, kind: str, category: str | None) -> None:
        self._current = f"{kind}:{category}" if category else kind
        await super().operation(kind, category)

    def report(self) -> dict[str, dict[str, Any]]:
        """Per-operation counts plus the informational codes each operation returned (content-free)."""
        return {
            name: {
                **counts,
                **({"informational_codes": sorted(set(codes))} if (codes := self.operation_codes.get(name)) else {}),
            }
            for name, counts in self.operations.items()
        }

    async def fhir(self, url: str) -> dict[str, Any]:
        body = await super().fhir(url)
        entries = body.get("entry")
        if body.get("resourceType") == "Bundle" and isinstance(entries, list):
            kept = []
            for entry in entries:
                codes = self._informational_outcome(entry)
                if codes is None:
                    kept.append(entry)
                else:
                    # Not a match: it is neither counted, retained nor validated as a resource.
                    self.outcome_codes.extend(codes)
                    if self._current is not None:
                        self.operation_codes.setdefault(self._current, []).extend(codes)
            body["entry"] = kept
        return body

    def retain_resource(self, resource: dict[str, Any], name: str) -> None:
        # Check nested patient references before a metadata projection removes fields.
        _check_source(resource, self.patient)
        super().retain_resource(resource, name)

    async def run(self) -> None:
        if self._ran:
            raise AcquisitionError("attempt_reused")
        self._ran = True
        try:
            self.checkpoint()
            if set(self.operations) != LIVE_OPERATIONS:
                raise AcquisitionError("operation_scope")
            if self.transport is None:
                self.transport = sandbox_transport()
            try:
                await super().run()
                self.candidate = normalize(self, self.alias, secrets.token_hex(16))
                manifest = self.candidate[0]
                manifest["completeness"] = COMPLETENESS
                manifest["synthetic_transport"] = self.injected_transport
                for name, codes in self.operation_codes.items():
                    manifest["coverage"]["operations"][name]["informational_codes"] = sorted(set(codes))
                if len(encoded(self.candidate[0])) + len(self.candidate[1]) > 20 * 1024 * 1024:
                    raise AcquisitionError("byte_limit")
                self.checkpoint()
            finally:
                # Cleanup is part of the attempt deadline, including on failure.
                async with asyncio.timeout(max(0, self.deadline - time.monotonic())):
                    await self.transport.aclose()
        except BaseException as exc:
            self.candidate = None
            if isinstance(exc, TimeoutError):
                raise AcquisitionError("deadline") from None
            if isinstance(exc, (AcquisitionError, RefreshRejected)) or not isinstance(exc, Exception):
                raise
            # Never expose transport URLs, bodies, tokens, keys or arbitrary fixture text.
            raise AcquisitionError("internal_error") from None
        finally:
            self.token = None
            self.resources = None
