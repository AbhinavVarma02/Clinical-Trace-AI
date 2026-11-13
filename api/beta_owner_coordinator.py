"""Default-disabled genuine-owner acquisition and candidate approval.

Reuse the genuine app's cookie-free HTTP boundary and durable WebAuthn verifier. One explicit immutable
plan can admit only one new attempt. Every acquisition/review/promotion result requires its own fresh
proof and returns directly. This module never loads credentials, provisions identity, enables LIVE reads,
removes an attempt guard, retries an attempt or enables clinical AI. Actual execution needs separate approval.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from api.beta_browser import BrowserAssets
from api.beta_live_record import LiveEvidenceBinding, _documents, _mapped_source, _resources
from api.beta_owner_genuine import BASE, DISPLAY_S, GenuineOwnerApp, _digest, _token
from api.beta_webauthn import MAX_PENDING
from api.beta_webauthn_store import OwnerCredentialStore
from src.clinical.fhir_models import normalize_timestamp, parse_timestamp
from src.interop.epic import beta_live_staging as files
from src.interop.epic import settings
from src.interop.epic.beta_acquisition import AcquisitionError, Limits, object_json
from src.interop.epic.beta_catalog import LIVE_POLICY_VERSION, RefreshRejected
from src.interop.epic.beta_live_acquisition import LIVE_OPERATIONS, LiveAcquisition, LiveRefresh
from src.interop.epic.beta_live_generations import LiveGenerations, check_tier
from src.interop.epic.beta_live_staging import LiveAllowlist, review_summary
from src.interop.epic.beta_staging import encoded
from src.interop.epic.keys import public_jwk


@dataclass(frozen=True)
class AcquisitionPlan:
    """Owner-reviewed one-use identity, exact patient, custody root and budgets; never supplied by HTTP."""

    approval_id: str = field(repr=False)
    alias: str
    patient: str = field(repr=False)
    private_root: Path
    limits: Limits = field(default_factory=Limits)

    def __post_init__(self) -> None:
        if not _token(self.approval_id) or not self.private_root.is_absolute() or type(self.limits) is not Limits:
            raise ValueError("exact new acquisition plan required")
        LiveAllowlist({self.alias: self.patient})

    @property
    def guard(self) -> Path:
        return self.private_root / ("genuine-acquisition-" + self.approval_id + ".consumed")


@dataclass(frozen=True)
class Pending:
    operation: str
    context: bytes = field(repr=False)
    binding: dict[str, str]
    expires: float


def _require_genuine(manifest: dict[str, Any]) -> None:
    if manifest.get("synthetic_transport") is not False:
        raise RefreshRejected(409, "genuine_candidate_required")


def _review_candidate(manifest: dict[str, Any], bundle: bytes, patient: str) -> dict[str, Any]:
    """Verify resource identity and document metadata before returning a bounded review."""
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
    except (AcquisitionError, ValueError, TypeError, KeyError, AttributeError):
        raise RefreshRejected(409, "candidate_integrity") from None


def _catalog_report(adapter: LiveAcquisition) -> dict[str, Any]:
    report = adapter.report()
    for operation, statuses in adapter.http_outcomes.items():
        report.setdefault(operation, {})["http_statuses"] = statuses
    return report


class GenuineOwnerCoordinatorApp(GenuineOwnerApp):
    """One event loop, direct responses, exact genuine store; not accepted by any existing launcher."""

    def __init__(
        self,
        store: OwnerCredentialStore,
        plan: AcquisitionPlan,
        live: LiveRefresh | None,
        generations: LiveGenerations,
        *,
        enabled: bool = False,
        policy_accepted: bool = False,
        acquisition_approved: bool = False,
        promotion_approved: bool = False,
        approved_candidate: LiveEvidenceBinding | None = None,
        assets: BrowserAssets | None = None,
    ) -> None:
        if enabled is not True or policy_accepted is not True:
            raise ValueError("genuine coordinator is disabled")
        if (
            type(plan) is not AcquisitionPlan
            or type(generations) is not LiveGenerations
            or type(acquisition_approved) is not bool
            or type(promotion_approved) is not bool
            or (acquisition_approved and live is None)
            or (promotion_approved and (acquisition_approved or type(approved_candidate) is not LiveEvidenceBinding))
            or (not promotion_approved and approved_candidate is not None)
            or (
                live is not None
                and (
                    type(live) is not LiveRefresh
                    or live.transport is not None
                    or dict(live.allowlist.patients) != {plan.alias: plan.patient}
                )
            )
            or dict(generations.allowlist.patients) != {plan.alias: plan.patient}
            or generations.root.parent != plan.private_root
            or generations.staging.root.parent != plan.private_root
            or generations.root == generations.staging.root
            or generations.staging.catalog is not generations.catalog
            or Path(generations.catalog.db.execute("PRAGMA database_list").fetchone()["file"]).parent
            != plan.private_root
        ):
            raise ValueError("one exact genuine acquisition/storage scope required")
        # The existing constructor refuses memory, synthetic-purpose or substitute durable state.
        super().__init__(store, None, False, assets, enabled=True, policy_accepted=True)
        self.plan, self.live, self.generations = plan, live, generations
        self.acquisition_approved, self.promotion_approved = acquisition_approved, promotion_approved
        self._approved_candidate = approved_candidate
        self._coordinator_pending: dict[str, Pending] = {}
        self._scope = self._scope_digest()
        self._client_scope = self._client_digest()

    def _client_digest(self) -> str | None:
        if self.live is None:
            return None
        return _digest([self.live.config.client_id, self.live.config.jku, public_jwk(self.live.key)["kid"]])

    def _scope_digest(self) -> str:
        return _digest(
            [
                "bounded-owner-acquisition-v2",
                self.plan.approval_id,
                self.plan.alias,
                self.plan.patient,
                str(self.plan.private_root),
                str(self.plan.guard),
                str(self.generations.root),
                str(self.generations.staging.root),
                self.generations.catalog.db.execute("PRAGMA database_list").fetchone()["file"],
                asdict(self.plan.limits),
                sorted(LIVE_OPERATIONS),
                settings.TOKEN_URL,
                settings.FHIR_BASE,
                self.policy.origin,
                self.policy.purpose,
                self.policy.owner_handle.hex(),
                self._store.principal,
                hashlib.sha256(self._auth._state.credential or b"").hexdigest(),
            ]
        )

    def _admit(self, operation: str) -> None:
        self._auth._owner_available()
        if (
            self._scope_digest() != self._scope
            or self._client_digest() != self._client_scope
            or dict(self.generations.allowlist.patients) != {self.plan.alias: self.plan.patient}
            or (
                self.live is not None
                and (
                    self.live.transport is not None
                    or dict(self.live.allowlist.patients) != {self.plan.alias: self.plan.patient}
                )
            )
            or self.generations.catalog.revoked("owner")
            or self.generations.catalog.revoked(self.plan.alias)
        ):
            raise RefreshRejected(403, "coordinator_scope_rejected")
        for path in (
            self.plan.private_root,
            Path(self.generations.catalog.db.execute("PRAGMA database_list").fetchone()["file"]),
            self.generations.root,
            self.generations.staging.root,
        ):
            files.verify_private_path(path)
        if operation == "acquire" and (self.acquisition_approved is not True or self.live is None):
            raise RefreshRejected(403, "acquisition_not_approved")
        if operation == "promote" and (
            self.promotion_approved is not True
            or self.generations.real_promotion is not True
            or self._approved_candidate is None
        ):
            raise RefreshRejected(403, "promotion_not_approved")

    def _candidate(self) -> tuple[dict[str, str], dict[str, Any], bytes]:
        try:
            files.verify_private_path(self.plan.guard)
            if files._is_link(self.plan.guard.lstat()) or self.plan.guard.stat().st_size > 2048:
                raise ValueError
            guard = object_json(self.plan.guard.read_bytes())
            if (
                set(guard) != {"version", "plan_sha256", "client_sha256", "consumed"}
                or type(guard["version"]) is not int
                or guard["version"] != 2
                or guard["plan_sha256"] != self._scope
                or guard["consumed"] is not True
                or not isinstance(guard["client_sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", guard["client_sha256"])
                or (self._client_scope is not None and guard["client_sha256"] != self._client_scope)
            ):
                raise ValueError
        except (AcquisitionError, OSError, ValueError):
            raise RefreshRejected(409, "acquisition_scope_mismatch") from None
        attempt = self.generations.catalog.existing_attempt(self.plan.alias, self.plan.approval_id)
        if attempt is None:
            raise RefreshRejected(404, "candidate_unavailable")
        try:
            row, raw, bundle = self.generations.staging.read_candidate(attempt, self.plan.alias)
            manifest = object_json(raw)
            check_tier(manifest, self.plan.alias, self.plan.patient, bundle)
            if manifest.get("attempt_id") != attempt:
                raise ValueError("attempt binding")
            _require_genuine(manifest)
            self.generations._fence(attempt, self.plan.alias)
        except (AcquisitionError, ValueError, TypeError, KeyError):
            raise RefreshRejected(409, "candidate_integrity") from None
        return (
            {
                "attempt_id": attempt,
                "generation_id": manifest["generation_id"],
                "manifest_sha256": row["manifest_sha256"],
                "bundle_sha256": manifest["bundle_sha256"],
            },
            manifest,
            bundle,
        )

    def _options(self, operation: str, body: dict[str, Any]) -> dict[str, Any]:
        self._admit(operation)
        binding: dict[str, str] = {}
        if operation == "acquire":
            if body:
                raise RefreshRejected(400, "unexpected_payload")
        else:
            binding, _, _ = self._candidate()
            if (operation == "review" and body) or (operation == "promote" and body != binding):
                raise RefreshRejected(409, "candidate_binding_required")
            if (
                operation == "promote"
                and self._approved_candidate is not None
                and asdict(self._approved_candidate) != binding
            ):
                raise RefreshRejected(409, "candidate_not_approved")
        now = self._auth.clock()
        self._coordinator_pending = {k: v for k, v in self._coordinator_pending.items() if v.expires > now}
        if len(self._coordinator_pending) >= MAX_PENDING:
            raise RefreshRejected(429, "authentication_busy")
        context = bytes.fromhex(
            _digest(
                [
                    "genuine-coordinator-request-v1",
                    operation,
                    ["POST", BASE + "/" + operation + "/finish", "", _digest(binding)],
                    self._scope,
                    self._process,
                    self.policy.origin,
                    self.policy.purpose,
                    self.policy.owner_handle.hex(),
                    self._store.principal,
                    hashlib.sha256(self._auth._state.credential or b"").hexdigest(),
                ]
            )
        )
        options = self._auth.begin_request(context)
        flow = options["flow_id"]
        self._coordinator_pending[flow] = Pending(operation, context, binding, self._auth._pending[flow].expires)
        return options

    def _current(self, operation: str, deadline: float) -> bool:
        self._admit(operation)
        return self._auth.request_current(deadline)

    async def _acquire(self, deadline: float) -> None:
        self._admit("acquire")
        assert self.live is not None and self._client_scope is not None

        def authorized() -> bool:
            return self._current("acquire", deadline)

        if not authorized():
            raise RefreshRejected(401, "owner_required")
        files.verify_private_path(self.plan.private_root)
        try:
            # Exclusive durable refusal marker precedes catalog admission/signing/transport. Never removed.
            files._write(
                self.plan.guard,
                encoded(
                    {"version": 2, "plan_sha256": self._scope, "client_sha256": self._client_scope, "consumed": True}
                ),
            )
            files._sync_directory(self.plan.private_root)
            files.verify_private_path(self.plan.guard)
        except FileExistsError:
            raise RefreshRejected(409, "acquisition_attempt_consumed") from None
        catalog, limits = self.generations.catalog, self.plan.limits
        if catalog.existing_attempt(self.plan.alias, self.plan.approval_id) is not None:
            raise RefreshRejected(409, "acquisition_attempt_consumed")
        attempt, created = catalog.admit(
            self.plan.alias,
            self.plan.approval_id,
            cooldown_s=limits.cooldown_s,
            patient_limit=limits.patient_per_hour,
            global_limit=limits.global_per_hour,
            request_limit=limits.global_requests_per_hour,
            policy=LIVE_POLICY_VERSION,
        )
        if not created:
            raise RefreshRejected(409, "acquisition_attempt_consumed")
        catalog.start(attempt)
        adapter: LiveAcquisition | None = None
        try:
            adapter = LiveAcquisition(
                catalog,
                attempt,
                self.plan.alias,
                self.live.key,
                self.live.config,
                allowlist=self.live.allowlist,
                enabled=True,
                authorized=authorized,
                limits=limits,
            )
            await adapter.run()
            if adapter.candidate is None or not authorized():
                raise RefreshRejected(401, "owner_required")
            manifest, bundle = adapter.candidate
            check_tier(manifest, self.plan.alias, self.plan.patient, bundle)
            if manifest.get("attempt_id") != attempt:
                raise AcquisitionError("attempt_scope")
            _require_genuine(manifest)
            _review_candidate(manifest, bundle, self.plan.patient)
            self.generations.staging.stage(attempt, self.plan.alias, encoded(manifest), bundle, authorized)
        except BaseException as exc:
            code = exc.code if isinstance(exc, (AcquisitionError, RefreshRejected)) else "acquisition_failed"
            catalog.finish(
                attempt,
                "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed",
                code,
                operations=_catalog_report(adapter) if adapter is not None else None,
            )
            raise
        else:
            catalog.finish(attempt, "succeeded", operations=_catalog_report(adapter))
        finally:
            if adapter is not None:
                adapter.candidate = None

    async def _finish(self, operation: str, body: dict[str, Any]) -> dict[str, Any]:
        if (
            set(body) != {"flow_id", "credential"}
            or not _token(body["flow_id"])
            or not isinstance(body["credential"], dict)
        ):
            raise RefreshRejected(400, "unexpected_payload")
        pending = self._coordinator_pending.pop(body["flow_id"], None)
        if pending is None:
            raise RefreshRejected(401, "ceremony_rejected")
        # Spend the actual verifier challenge before scope/candidate validation or any side effect.
        expires = self._auth.complete_request(body["flow_id"], pending.context, body["credential"])
        if pending.operation != operation:
            raise RefreshRejected(401, "ceremony_rejected")
        self._admit(operation)
        if operation == "acquire":
            await self._acquire(expires)
        binding, manifest, bundle = self._candidate()
        if operation != "acquire" and binding != pending.binding:
            raise RefreshRejected(409, "candidate_changed")
        if (
            operation == "promote"
            and self._approved_candidate is not None
            and asdict(self._approved_candidate) != binding
        ):
            raise RefreshRejected(409, "candidate_not_approved")
        deadline = min(expires, self._auth.clock() + DISPLAY_S)
        result: dict[str, Any] = {
            "operation": operation,
            "alias": self.plan.alias,
            "patient_identifier": self.plan.patient,
            "provenance": binding,
            "synthetic_transport": manifest["synthetic_transport"],
            "review": _review_candidate(manifest, bundle, self.plan.patient),
            "live_serving": "disabled",
            "clinical_ai": "disabled",
        }
        if operation == "promote":
            gid = binding["generation_id"]
            root = self.generations.root
            if any(
                os.path.lexists(path)
                for path in (
                    root / "tmp" / f"{gid}.check",
                    root / "tmp" / f"{gid}.obj",
                    root / "objects" / gid,
                )
            ):
                raise RefreshRejected(409, "promotion_storage_occupied")
            # Callback can be consumed only by this approve call, and cannot create a serving grant.
            fresh = True

            def consume() -> bool:
                nonlocal fresh
                value, fresh = fresh, False
                return value and self._current(operation, expires)

            result["generation"] = self.generations.approve(
                self.plan.alias,
                binding["attempt_id"],
                binding["manifest_sha256"],
                lambda: self._current(operation, expires),
                consume,
            )
        payload = {
            "result": result,
            "server_time": self._auth.clock(),
            "display_until": deadline,
            "coordinator_operation": operation,
        }
        self._return_current(payload)
        return payload

    async def _dispatch_action(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        if action == "operations":
            super()._action(action, body)
            return {
                "actions": [
                    *(["acquire"] if self.acquisition_approved else []),
                    "review",
                    *(["promote"] if self.promotion_approved else []),
                ]
            }
        operation, _, stage = action.partition("/")
        if operation not in {"acquire", "review", "promote"} or stage not in {"options", "finish"}:
            return await super()._dispatch_action(action, body)
        return self._options(operation, body) if stage == "options" else await self._finish(operation, body)

    def _return_current(self, payload: dict[str, Any]) -> None:
        operation = payload.get("coordinator_operation")
        if operation is None:
            return super()._return_current(payload)
        if not self._current(operation, payload["display_until"]):
            raise RefreshRejected(401, "owner_required")
        expected = payload["result"]["provenance"]
        if operation == "promote":

            def binding(manifest: dict, bundle: bytes, row: dict) -> dict:
                _require_genuine(manifest)
                return {
                    "attempt_id": row["attempt_id"],
                    "generation_id": manifest["generation_id"],
                    "manifest_sha256": row["manifest_sha256"],
                    "bundle_sha256": manifest["bundle_sha256"],
                }

            actual = self.generations.project_active(
                self.plan.alias, lambda: self._current(operation, payload["display_until"]), binding
            )
        else:
            actual, _, _ = self._candidate()
        if actual != expected:
            raise RefreshRejected(409, "candidate_changed")


def prepare_coordinator_https(
    app: GenuineOwnerCoordinatorApp, certificate: Path, key: Path, *, enabled: bool = False
) -> Any:
    """Prepare only after separate transport/credential/storage/action approvals; never bind or launch."""
    if enabled is not True or type(app) is not GenuineOwnerCoordinatorApp:
        raise ValueError("genuine coordinator HTTPS is disabled")
    app._auth._owner_available()
    from api.beta_owner_https import prepare_bootstrap

    config = prepare_bootstrap(app.policy, certificate, key, enabled=True)
    config.app = app
    config.loaded_app = app
    return config
