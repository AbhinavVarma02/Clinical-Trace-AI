"""Hosted, owner-only LIVE Epic Beta backend: fresh Epic sandbox retrieval behind Cloudflare Access.

Composition only. Every step reuses the accepted local genuine path:

    Access-verified owner request
      -> Catalog admission (cooldown, per-patient and global budgets, single in-flight attempt)
      -> LiveAcquisition (RS384 private_key_jwt OAuth, the eight read-only operations, patient-bound, host-pinned)
      -> check_tier + genuine-transport gate + candidate review   (same checks as the owner coordinator)
      -> LiveStaging.stage
      -> owner Approve with the exact attempt and manifest hash  -> LiveGenerations.approve (real promotion)
      -> live_record_view(genuine_only=True)                     -> the Beta UI

Differences from the local owner path, all deliberate: Windows Hello and the loopback origin are replaced by a
verified Cloudflare Access application token for one configured owner email; storage is the container's private,
ephemeral disk; each Refresh admits a new attempt under the catalog's existing budgets. Clinical AI, publication,
note bodies, Binary and MedicationRequest stay off. The only patient is the single configured allowlist entry.

Nothing here runs unless ``EPIC_SANDBOX_ENABLED=1`` is set for the process (the hosted kill switch). Invented
application fixtures can never be displayed: a fake transport marks its candidate ``synthetic_transport`` and both
the staging gate and the projection refuse it.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from api.beta_hosted.access import AccessPolicy, AccessRejected, AccessVerifier
from api.beta_live_record import _documents, _mapped_source, _operations, _resources, live_record_view
from api.beta_record import _attempt
from src.clinical.fhir_models import normalize_timestamp, parse_timestamp
from src.interop.epic import settings
from src.interop.epic.beta_acquisition import AcquisitionError, Limits, object_json
from src.interop.epic.beta_catalog import LIVE_POLICY_VERSION, Catalog, RefreshRejected
from src.interop.epic.beta_live_acquisition import LiveAcquisition
from src.interop.epic.beta_live_generations import LiveGenerations, check_tier
from src.interop.epic.beta_live_staging import ALIAS, ATTEMPT, LiveAllowlist, LiveStaging, private_root, review_summary
from src.interop.epic.beta_staging import encoded
from src.interop.epic.client import valid_fhir_id

BASE = "/beta/api"
CLIENT_HEADER = ("x-beta-client", "live")
BODY_LIMIT = 4096
CLIENT_ID = re.compile(r"[A-Za-z0-9-]{8,128}")
HASH = re.compile(r"[a-f0-9]{64}")
WORKERS_ORIGIN = re.compile(r"https://[a-z0-9-]{1,63}\.[a-z0-9-]{1,63}\.workers\.dev")
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}
HOSTED_WARNING = {
    "code": "hosted_owner_only",
    "severity": "info",
    "message": "Owner-only hosted Beta. Retrieved from the Epic FHIR R4 sandbox by the hosted backend; not for public display.",
}
CLINICAL_AI_WARNING = {
    "code": "clinical_ai_disabled",
    "severity": "warning",
    "message": "Clinical AI and publication remain disabled.",
}
ENV = {
    "team": "BETA_ACCESS_TEAM_DOMAIN",
    "aud": "BETA_ACCESS_AUD",
    "owner": "BETA_OWNER_EMAIL",
    "origin": "BETA_ORIGIN",
    "alias": "BETA_PATIENT_ALIAS",
    "patient": "BETA_PATIENT_ID",
    "client": "EPIC_CLIENT_ID",
    "jku": "EPIC_JKU",
    "root": "BETA_DATA_ROOT",
}


def _https_url(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or any(char.isspace() for char in value)
    ):
        raise ValueError("EPIC_JKU must be a plain https URL")
    return value


@dataclass(frozen=True)
class HostedConfig:
    access: AccessPolicy
    origin: str
    alias: str
    patient: str
    client_id: str = field(repr=False)
    jku: str | None
    data_root: Path

    def __post_init__(self) -> None:
        if not WORKERS_ORIGIN.fullmatch(self.origin or ""):
            raise ValueError("BETA_ORIGIN must be the hosted Beta's https workers.dev origin")
        if not ALIAS.fullmatch(self.alias or "") or self.alias.startswith("mock-"):
            raise ValueError("the patient alias is malformed")
        valid_fhir_id(self.patient)
        if self.patient in {".", ".."}:
            raise ValueError("the patient id is malformed")
        if not CLIENT_ID.fullmatch(self.client_id or ""):
            raise ValueError("the Epic client id is malformed")
        if self.jku is not None:
            _https_url(self.jku)
        if not self.data_root.is_absolute():
            raise ValueError("the data root must be absolute")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> HostedConfig:
        source = os.environ if env is None else env
        value = {name: (source.get(variable) or "").strip() for name, variable in ENV.items()}
        missing = [ENV[name] for name in ("team", "aud", "owner", "origin", "alias", "patient", "client") if not value[name]]
        if missing:
            raise ValueError("missing hosted configuration: " + ", ".join(missing))
        return cls(
            AccessPolicy(value["team"], value["aud"], value["owner"]),
            value["origin"],
            value["alias"],
            value["patient"],
            value["client"],
            value["jku"] or None,
            Path(value["root"] or "/data"),
        )


def review_candidate(manifest: dict[str, Any], bundle: bytes, patient: str) -> dict[str, Any]:
    """The owner coordinator's candidate review (``api.beta_owner_coordinator._review_candidate``), unchanged.

    Duplicated rather than imported so the hosted image does not load the Windows Hello owner stack; a test pins the
    two to identical output.
    """
    try:
        resources = _resources(manifest, bundle)
        notes = _documents(manifest, patient)
        patient_resource = next(resource for resource in resources if resource["resourceType"] == "Patient")
        name = (patient_resource.get("name") or [{}])[0]
        display_name = (name.get("text") or f"{' '.join(name.get('given', []))} {name.get('family', '')}").strip()
        timestamps: dict[str, list[str]] = {}
        for resource in resources:
            timestamp = _mapped_source(resource)["last_updated"]
            if timestamp:
                timestamps.setdefault(resource["resourceType"], []).append(timestamp)
        for document in manifest["document_metadata"]:
            timestamp = (document.get("meta") or {}).get("lastUpdated")
            if timestamp:
                timestamps.setdefault("DocumentReference", []).append(normalize_timestamp(timestamp))
        return review_summary(manifest) | {
            "patient_display_name": display_name[:200] or None,
            "document_metadata_count": notes["total"],
            "mapping": manifest["coverage"]["mapping"],
            "http_outcomes": manifest["coverage"].get("http_outcomes", {}),
            "source_timestamps": {
                kind: {
                    "count": len(values),
                    "oldest": min(values, key=parse_timestamp),
                    "newest": max(values, key=parse_timestamp),
                }
                for kind, values in sorted(timestamps.items())
            },
        }
    except (AcquisitionError, ValueError, TypeError, KeyError, AttributeError, StopIteration):
        raise RefreshRejected(409, "candidate_integrity") from None


def _require_genuine(manifest: dict[str, Any]) -> None:
    if manifest.get("synthetic_transport") is not False:
        raise RefreshRejected(409, "genuine_candidate_required")


def _operations_report(adapter: LiveAcquisition) -> dict[str, Any]:
    report = adapter.report()
    for operation, statuses in adapter.http_outcomes.items():
        report.setdefault(operation, {})["http_statuses"] = statuses
    return report


def _hosted_view(view: dict[str, Any]) -> dict[str, Any]:
    """Serve the verified LIVE projection to its owner: the same relabelling the genuine owner read applies."""
    view["serving"] = view["freshness"]
    view["warnings"] = [warning for warning in view["warnings"] if warning["code"] != "live_access_disabled"]
    view["warnings"][:0] = [HOSTED_WARNING]
    view["warnings"].append(CLINICAL_AI_WARNING)
    return view


class Hosted:
    """All state lives on the one app loop; ``lock`` serializes every state-changing route."""

    catalog: Catalog
    staging: LiveStaging
    generations: LiveGenerations

    def __init__(self, config: HostedConfig, key: rsa.RSAPrivateKey, verifier: AccessVerifier, limits: Limits,
                 clock: Callable[[], float], epic_transport: httpx.MockTransport | None) -> None:
        self.config, self.key, self.verifier, self.limits, self.clock = config, key, verifier, limits, clock
        self.epic_transport = epic_transport
        self.allowlist = LiveAllowlist({config.alias: config.patient})
        # private_key_path is never read: the hosted key arrives already parsed from the secret store.
        self.epic = settings.EpicConfig(config.client_id, Path("/nonexistent/hosted-secret-store"), config.jku)
        self.lock = asyncio.Lock()

    def open(self) -> None:
        root = private_root(self.config.data_root)
        self.catalog = Catalog(root / "catalog.sqlite", self.clock)
        try:
            self.staging = LiveStaging(self.catalog, root / "staging")
            self.generations = LiveGenerations(
                self.catalog, root / "generations", self.allowlist, self.staging, real_promotion=True
            )
        except BaseException:
            self.catalog.close()
            raise

    def close(self) -> None:
        self.catalog.close()

    def purge(self) -> None:
        self.staging.purge()
        self.generations.purge()

    # -- routes ---------------------------------------------------------------------------------------------

    def session(self, expires: float) -> dict[str, Any]:
        enabled = os.environ.get(settings.ENABLE_ENV) == "1"
        return {
            "owner": True,
            "mode": "LIVE",
            "source": settings.SOURCE_SYSTEM,
            "patient": {"alias": self.config.alias},
            "epic_enabled": enabled,
            "server_time": self.clock(),
            "access_expires": expires,
        }

    def status(self) -> dict[str, Any]:
        latest = self.catalog.status(self.config.alias, live=True)["attempt"]
        attempt = _attempt(latest)
        if attempt is not None:
            attempt["operations"] = _operations(latest["operations"])
        candidate = self.staging.view(self.config.alias)
        return {
            "attempt": attempt,
            "candidate": {
                key: candidate.get(key) for key in ("attempt_id", "state", "manifest_sha256", "expires_at", "review")
            }
            if candidate
            else None,
            "generation": self.generations.view(self.config.alias),
        }

    def record(self, authorized: Callable[[], bool]) -> dict[str, Any]:
        return _hosted_view(live_record_view(self.generations, self.config.alias, authorized=authorized, genuine_only=True))

    async def refresh(self, authorized: Callable[[], bool]) -> dict[str, Any]:
        try:
            settings.require_enabled()
        except settings.SandboxDisabled:
            raise RefreshRejected(503, "live_disabled") from None
        alias = self.config.alias
        attempt, _ = self.catalog.admit(
            alias,
            secrets.token_urlsafe(24),
            cooldown_s=self.limits.cooldown_s,
            patient_limit=self.limits.patient_per_hour,
            global_limit=self.limits.global_per_hour,
            request_limit=self.limits.global_requests_per_hour,
            policy=LIVE_POLICY_VERSION,
        )
        self.catalog.start(attempt)
        adapter: LiveAcquisition | None = None
        try:
            adapter = LiveAcquisition(
                self.catalog,
                attempt,
                alias,
                self.key,
                self.epic,
                allowlist=self.allowlist,
                enabled=True,
                authorized=authorized,
                limits=self.limits,
                transport=self.epic_transport,
            )
            await adapter.run()
            if adapter.candidate is None or not authorized():
                raise RefreshRejected(401, "owner_required")
            manifest, bundle = adapter.candidate
            check_tier(manifest, alias, self.config.patient, bundle)
            if manifest.get("attempt_id") != attempt:
                raise AcquisitionError("attempt_scope")
            _require_genuine(manifest)
            review = review_candidate(manifest, bundle, self.config.patient)
            self.staging.stage(attempt, alias, encoded(manifest), bundle, authorized)
            row, _, _ = self.staging.read_candidate(attempt, alias)
        except BaseException as exc:
            code = exc.code if isinstance(exc, (AcquisitionError, RefreshRejected)) else "acquisition_failed"
            self.catalog.finish(
                attempt,
                "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed",
                code,
                operations=_operations_report(adapter) if adapter is not None else None,
            )
            if isinstance(exc, AcquisitionError):
                raise RefreshRejected(503 if exc.code == "live_disabled" else 502, exc.code) from None
            if isinstance(exc, ValueError):
                raise RefreshRejected(409, "candidate_integrity") from None
            raise
        else:
            self.catalog.finish(attempt, "succeeded", operations=_operations_report(adapter))
        finally:
            if adapter is not None:
                adapter.candidate = None
        return {
            "alias": alias,
            "patient_identifier": self.config.patient,
            "candidate": {
                "attempt_id": attempt,
                "generation_id": manifest["generation_id"],
                "manifest_sha256": row["manifest_sha256"],
                "bundle_sha256": manifest["bundle_sha256"],
            },
            "synthetic_transport": manifest["synthetic_transport"],
            "review": review,
            "status": self.status(),
        }

    def approve(self, body: dict[str, Any], authorized: Callable[[], bool]) -> dict[str, Any]:
        if set(body) != {"attempt_id", "manifest_sha256"}:
            raise RefreshRejected(400, "unexpected_payload")
        attempt, digest = body["attempt_id"], body["manifest_sha256"]
        if not isinstance(attempt, str) or not ATTEMPT.fullmatch(attempt):
            raise RefreshRejected(400, "unexpected_payload")
        if not isinstance(digest, str) or not HASH.fullmatch(digest):
            raise RefreshRejected(400, "candidate_hash_required")
        alias = self.config.alias
        try:
            row, raw, bundle = self.staging.read_candidate(attempt, alias)
            manifest = object_json(raw)
            check_tier(manifest, alias, self.config.patient, bundle)
            if manifest.get("attempt_id") != attempt or not secrets.compare_digest(row["manifest_sha256"], digest):
                raise ValueError("candidate binding")
        except AcquisitionError as exc:
            raise RefreshRejected(404 if exc.code == "candidate_unavailable" else 409, exc.code) from None
        except (ValueError, TypeError, KeyError):
            raise RefreshRejected(409, "candidate_integrity") from None
        _require_genuine(manifest)
        spent = False

        def fresh() -> bool:  # one promotion per verified owner request
            nonlocal spent
            value, spent = not spent, True
            return value and authorized()

        self.generations.approve(alias, attempt, digest, authorized, fresh)
        return self.record(authorized)

    def erase(self) -> None:
        self.staging.erase_all("owner_erased")
        self.generations.erase_all("owner_erased")


def _reject(exc: RefreshRejected) -> JSONResponse:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
    return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status, headers=headers)


async def _body(request: Request) -> bytes:
    # Bounded by BODY_LIMIT whether the hop delivers it with a length or chunked.
    raw = bytearray()
    try:
        async with asyncio.timeout(2):
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > BODY_LIMIT:
                    raise RefreshRejected(413, "payload_too_large")
    except TimeoutError:
        raise RefreshRejected(400, "unexpected_payload") from None
    return bytes(raw)


ROUTES: dict[tuple[str, str], str] = {
    ("GET", BASE + "/health"): "health",
    ("GET", BASE + "/session"): "session",
    ("GET", BASE + "/status"): "status",
    ("GET", BASE + "/record"): "record",
    ("POST", BASE + "/refresh"): "refresh",
    ("POST", BASE + "/approve"): "approve",
    ("POST", BASE + "/erase"): "erase",
}


def create_hosted_app(
    config: HostedConfig,
    key: rsa.RSAPrivateKey,
    *,
    clock: Callable[[], float] = time.time,
    limits: Limits | None = None,
    access_transport: httpx.AsyncBaseTransport | None = None,
    epic_transport: httpx.MockTransport | None = None,
) -> FastAPI:
    """``epic_transport`` is for offline tests only: a MockTransport, whose candidates are refused for display."""
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise ValueError("the hosted signing key must be RSA with at least 2048 bits")
    if epic_transport is not None and not isinstance(epic_transport, httpx.MockTransport):
        raise ValueError("only an offline MockTransport may replace the real Epic transport")
    hosted = Hosted(config, key, AccessVerifier(config.access, access_transport, clock), limits or Limits(), clock,
                    epic_transport)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        hosted.open()
        try:
            yield
        finally:
            hosted.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.state.hosted = hosted

    async def dispatch(request: Request) -> Response:
        method, path = request.method, request.scope["path"]
        name = ROUTES.get((method, path))
        if name is None:
            known = any(route_path == path for _, route_path in ROUTES)
            raise RefreshRejected(405 if known else 404, "method_not_allowed" if known else "not_found")
        if request.scope.get("query_string") or request.scope.get("raw_path", path.encode()) != path.encode():
            raise RefreshRejected(400, "unexpected_payload")
        if name == "health":
            await _body(request)
            return JSONResponse({"status": "ok"})
        try:
            identity = await hosted.verifier.verify(request.headers.getlist("cf-access-jwt-assertion"))
        except AccessRejected as exc:
            hosted.catalog.audit("access_" + exc.code)
            raise RefreshRejected(401, "owner_required") from None

        def authorized() -> bool:
            return hosted.clock() < identity.expires and not hosted.catalog.revoked("owner")

        # Cross-site requests cannot set this header without a CORS preflight, which is never answered. POSTs must
        # also carry the exact origin.
        if request.headers.getlist(CLIENT_HEADER[0]) != [CLIENT_HEADER[1]] or (
            method == "POST" and request.headers.getlist("origin") != [config.origin]
        ):
            raise RefreshRejected(403, "same_origin_required")
        raw = await _body(request)
        body: dict[str, Any] = {}
        if name == "approve":
            if request.headers.getlist("content-type") != ["application/json"]:
                raise RefreshRejected(415, "unexpected_payload")
            try:
                body = object_json(raw)
            except (AcquisitionError, ValueError):
                raise RefreshRejected(400, "unexpected_payload") from None
        elif raw:
            raise RefreshRejected(400, "unexpected_payload")
        hosted.purge()
        if name == "session":
            return JSONResponse(hosted.session(identity.expires))
        if name == "status":
            return JSONResponse(hosted.status())
        if name == "record":
            return JSONResponse(hosted.record(authorized))
        async with hosted.lock:
            if name == "refresh":
                return JSONResponse(await hosted.refresh(authorized))
            if name == "approve":
                return JSONResponse(hosted.approve(body, authorized))
            hosted.erase()
            return Response(status_code=204)

    @app.middleware("http")
    async def boundary(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        try:
            response = await dispatch(request)
        except RefreshRejected as exc:
            try:
                hosted.catalog.audit(exc.code)
            except Exception:
                pass
            response = _reject(exc)
        except Exception:
            try:
                hosted.catalog.audit("internal_error")
            except Exception:
                pass
            response = _reject(RefreshRejected(500, "internal_error"))
        response.headers.update(SECURITY_HEADERS)
        return response

    return app


def main() -> None:  # pragma: no cover - container entry point
    """Container entry point: configuration and the key come only from the host's environment and secret store."""
    import uvicorn

    from api.beta_hosted.hosted_keys import take_key

    os.umask(0o077)
    key = take_key()
    config = HostedConfig.from_env()
    for variable in ("EPIC_CLIENT_ID",):
        os.environ.pop(variable, None)  # parsed above; not needed again by any code
    app = create_hosted_app(config, key)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        proxy_headers=False,
        server_header=False,
        date_header=False,
        access_log=False,
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
