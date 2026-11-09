"""Offline LIVE-adapter contracts. All records, keys, tokens and transports are invented."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.interop.epic import beta_catalog, settings
from src.interop.epic.auth import CLIENT_ASSERTION_TYPE
from src.interop.epic.beta_acquisition import MOCK_SCOPES, AcquisitionError, Limits
from src.interop.epic.beta_catalog import Catalog, RefreshRejected
from src.interop.epic.beta_live_acquisition import (
    LIVE_OPERATIONS,
    LiveAcquisition,
    sandbox_transport,
)
from src.interop.epic.beta_live_staging import LiveAllowlist
from src.interop.epic.beta_staging import encoded
from src.interop.epic.mapping import content_sha256, internal_id
from tests.test_beta_generations import BODY, fixture_resources

PATIENT = "invented-live-alpha"
OTHER = "invented-live-bravo"
ALIAS = "approved-alpha"
TOKEN = "invented-live-token"


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def live(tmp_path, monkeypatch, key):
    monkeypatch.setenv(settings.ENABLE_ENV, "1")

    class Harness:
        def __init__(self):
            self.catalog = Catalog(tmp_path / "control.sqlite", lambda: 1_799_000_000.0)
            self.allowlist = LiveAllowlist({ALIAS: PATIENT})
            self.attempt, _ = self.catalog.admit(
                ALIAS,
                "invented-live-attempt",
                cooldown_s=300,
                patient_limit=2,
                global_limit=6,
                request_limit=240,
                policy=beta_catalog.LIVE_POLICY_VERSION,
            )
            self.catalog.start(self.attempt)
            self.config = settings.EpicConfig(
                "invented-live-client", tmp_path / "never-read.key", "https://keys.invalid/jwks"
            )
            self.calls = []
            self.records = fixture_resources(PATIENT)
            self.override = None
            self.allowed = True
            self.scopes = " ".join(sorted(MOCK_SCOPES))

        async def respond(self, request):
            self.calls.append(request)
            if self.override is not None:
                response = await self.override(request)
                if response is not None:
                    return response
            if request.method == "POST":
                return httpx.Response(
                    200,
                    json={
                        "access_token": TOKEN,
                        "token_type": "Bearer",
                        "expires_in": 300,
                        "scope": self.scopes,
                    },
                )
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
            if request.url.path == settings.FHIR_BASE_PATH + f"/Patient/{PATIENT}":
                return httpx.Response(200, json=self.records[0])
            kind = request.url.path.rsplit("/", 1)[1]
            assert request.url.params["patient"] == PATIENT
            category = request.url.params.get("category")
            selected = [r for r in self.records if r["resourceType"] == kind]
            if category:
                selected = [
                    r
                    for r in selected
                    if any(coding["code"] == category for concept in r["category"] for coding in concept["coding"])
                ]
            return httpx.Response(
                200,
                json={
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "total": len(selected),
                    "entry": [{"resource": r} for r in selected],
                },
            )

        def adapter(self, **kwargs):
            options = dict(
                allowlist=self.allowlist,
                enabled=True,
                authorized=lambda: self.allowed,
                transport=httpx.MockTransport(self.respond),
            )
            options.update(kwargs)
            return LiveAcquisition(self.catalog, self.attempt, ALIAS, key, self.config, **options)

        def run(self, **kwargs):
            adapter = self.adapter(**kwargs)
            asyncio.run(adapter.run())
            return adapter

    h = Harness()
    yield h
    h.catalog.close()


def test_default_disabled_and_empty_approved_patient_list(live, monkeypatch):
    with pytest.raises(AcquisitionError, match="^live_disabled$"):
        live.adapter(enabled=False)
    monkeypatch.delenv(settings.ENABLE_ENV)
    with pytest.raises(AcquisitionError, match="^live_disabled$"):
        live.adapter()
    monkeypatch.setenv(settings.ENABLE_ENV, "1")
    with pytest.raises(AcquisitionError, match="^patient_not_approved$"):
        live.adapter(allowlist=LiveAllowlist.empty())
    assert live.calls == []


def test_omitted_enable_flag_never_constructs_transport(live, monkeypatch, key):
    monkeypatch.setattr(
        "src.interop.epic.beta_live_acquisition.sandbox_transport", lambda: pytest.fail("transport opened")
    )
    with pytest.raises(AcquisitionError, match="live_disabled"):
        LiveAcquisition(
            live.catalog, live.attempt, ALIAS, key, live.config, allowlist=live.allowlist, authorized=lambda: True
        )


@pytest.mark.parametrize(
    "alias,patient",
    [
        (ALIAS, "."),
        (ALIAS, ".."),
        (ALIAS, "../other"),
        ("mock-alpha", PATIENT),
        (ALIAS, beta_catalog.MOCK_PATIENTS["mock-alpha"]),
    ],
)
def test_invalid_or_mock_identity_can_never_be_allowlisted(live, alias, patient):
    with pytest.raises(ValueError):
        LiveAllowlist({alias: patient})
    assert live.calls == []


@pytest.mark.parametrize(
    "change,code",
    [
        ("alias", "attempt_scope"),
        ("policy", "attempt_scope"),
        ("state", "attempt_scope"),
        ("owner", "authorization_revoked"),
        ("patient", "authorization_revoked"),
        ("callback", "authorization_revoked"),
    ],
)
def test_admitted_attempt_and_authorization_are_required(live, change, code):
    if change in {"alias", "policy", "state"}:
        live.catalog.db.execute(f"UPDATE attempt SET {change} = 'invented-mismatch'")
    elif change == "callback":
        live.allowed = False
    else:
        live.catalog.revoke("owner" if change == "owner" else ALIAS)
    with pytest.raises(AcquisitionError, match=code):
        live.adapter()
    assert live.calls == []


def test_all_eight_operations_map_and_preserve_synthetic_provenance(live):
    adapter = live.run()
    manifest, raw = adapter.candidate
    assert set(adapter.operations) - {"MedicationRequest", "Binary"} == LIVE_OPERATIONS
    assert len(live.calls) == 9
    assert manifest["mode"] == "MOCK" and manifest["source"] == "epic-sandbox-mock"
    assert manifest["policy"] == beta_catalog.LIVE_POLICY_VERSION
    assert manifest["manifest_version"] == "epic-acquisition/1"
    assert manifest["coverage"]["operations"]["Binary"]["execution"] == "not_requested"
    assert manifest["coverage"]["operations"]["MedicationRequest"]["execution"] == "not_requested"
    document = next(r for r in live.calls if r.url.path.endswith("/DocumentReference"))
    assert set(document.url.params) == {"patient", "category"}
    searches = [r for r in live.calls if r.method == "GET" and r.url.query]
    assert len(searches) == 7 and all(set(r.url.params) <= {"patient", "category"} for r in searches)
    assert BODY.encode() not in raw + encoded(manifest)
    assert b"Binary/secret" not in raw + encoded(manifest)
    resources = [entry["resource"] for entry in json.loads(raw)["entry"]]
    assert {r["subject"]["reference"] for r in resources if "subject" in r} == {
        f"Patient/{internal_id('Patient', PATIENT)}"
    }
    observation = next(r for r in resources if r["id"] == internal_id("Observation", "laboratory"))
    provenance = {p["url"]: p for p in observation["extension"][0]["extension"]}
    source = next(r for r in live.records if r["id"] == "laboratory")
    assert provenance["sourceContentSha256"]["valueString"] == content_sha256(source)
    assert observation["effectiveDateTime"] == source["effectiveDateTime"]
    assert observation["meta"]["lastUpdated"] == source["meta"]["lastUpdated"]
    assert adapter.resources is None and adapter.token is None
    assert not live.config.private_key_path.exists()
    # The attempt catalog contains only content-free control information.
    dump = "\n".join(live.catalog.db.iterdump())
    assert PATIENT not in dump and TOKEN not in dump and BODY not in dump


def test_network_branch_uses_live_provenance_and_closes_owned_transport(live, monkeypatch):
    class FakeWire(httpx.MockTransport):
        closed = False

        async def aclose(self):
            self.closed = True

    wire = FakeWire(live.respond)
    monkeypatch.setattr("src.interop.epic.beta_live_acquisition.sandbox_transport", lambda: wire)
    adapter = live.run(transport=None)
    assert wire.closed
    assert adapter.candidate[0]["mode"] == "LIVE"
    assert adapter.candidate[0]["source"] == settings.SOURCE_SYSTEM


def test_real_transport_configuration_is_fixed(monkeypatch):
    seen = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return "fake-transport"

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", capture)
    assert sandbox_transport() == "fake-transport"
    assert seen["verify"] is True and seen["trust_env"] is False and seen["retries"] == 0
    assert seen["limits"].max_connections == 1 and seen["limits"].max_keepalive_connections == 0


def test_signed_oauth_reuses_registered_identity_and_jku(live, key):
    live.run()
    request = live.calls[0]
    assert str(request.url) == settings.TOKEN_URL
    form = parse_qs(request.content.decode())
    assert form["grant_type"] == ["client_credentials"]
    assert form["client_assertion_type"] == [CLIENT_ASSERTION_TYPE]
    head, claims, signature = form["client_assertion"][0].split(".")
    key.public_key().verify(
        base64.urlsafe_b64decode(signature + "=="), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA384()
    )
    header = json.loads(base64.urlsafe_b64decode(head + "=="))
    body = json.loads(base64.urlsafe_b64decode(claims + "=="))
    assert header["jku"] == live.config.jku
    assert body["iss"] == body["sub"] == live.config.client_id
    assert body["aud"] == settings.TOKEN_URL


@pytest.mark.parametrize("scope", ["", "system/*.read", "system/Patient.write", "extra"])
def test_unapproved_or_missing_grants_stop_before_fhir(live, scope):
    live.scopes = {
        "extra": live.scopes + " system/Patient.read.extra",
    }.get(scope, scope)
    with pytest.raises(AcquisitionError, match="unapproved_grant"):
        live.run()
    assert len(live.calls) == 1


def test_historical_registration_grants_do_not_enable_extra_operations(live):
    live.scopes += " system/Binary.read system/MedicationRequest.read"
    live.run()
    assert len(live.calls) == 9
    assert not any("/Binary" in r.url.path or "/MedicationRequest" in r.url.path for r in live.calls)


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/Binary/secret"),
        ("GET", "/MedicationRequest?patient=" + PATIENT),
        ("GET", "/Patient?name=Invented"),
        ("GET", "/DocumentReference/metadata"),
        ("GET", "/Observation?patient=" + OTHER + "&category=laboratory"),
        ("GET", "/Observation?patient=" + PATIENT + "&category=social-history"),
        ("GET", "/Encounter?patient=" + PATIENT + "&_include=*"),
        ("POST", "/Encounter"),
        ("DELETE", "/Patient/" + PATIENT),
    ],
)
def test_additional_operations_are_denied_before_token(live, method, path):
    adapter = live.adapter()
    with pytest.raises(AcquisitionError, match="operation_scope"):
        asyncio.run(adapter.request(method, settings.FHIR_BASE + path))
    assert live.calls == []


@pytest.mark.parametrize("suffix", ["&_cursor=invented-2", "&page=2"])
def test_patient_bound_pagination(live, suffix):
    async def pages(request):
        if (
            request.url.path.endswith("/Encounter")
            and "page" not in request.url.params
            and "_cursor" not in request.url.params
        ):
            # Reordered filters are canonicalized before dispatch.
            return httpx.Response(
                200,
                json={
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "total": 1,
                    "link": [{"relation": "next", "url": str(request.url) + suffix}],
                },
            )

    live.override = pages
    adapter = live.run()
    assert adapter.operations["Encounter"]["pages"] == 2
    assert len(live.calls) == 10


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.invalid/Encounter?patient=" + PATIENT + "&page=2",
        settings.FHIR_BASE + "/Encounter?patient=" + OTHER + "&page=2",
        settings.FHIR_BASE + "/Encounter?page=2",
        settings.FHIR_BASE + "/Encounter?patient=" + PATIENT + "&patient=" + PATIENT + "&page=2",
        settings.FHIR_BASE + "/Encounter?patient=" + PATIENT + "&_include=*&page=2",
        settings.FHIR_BASE + "/Encounter?patient=" + PATIENT + "&page=2#fragment",
        settings.FHIR_BASE + "/Encounter?patient=" + PATIENT + "&_cursor=bad%2Fcursor",
        settings.FHIR_BASE + "/Encounter?patient=" + PATIENT + "&page=-1",
        settings.FHIR_BASE + "/Encounter?patient=" + PATIENT + "&search-id=opaque",
    ],
)
def test_cross_scope_or_unknown_pages_fail_closed(live, url):
    async def pages(request):
        if request.url.path.endswith("/Encounter"):
            return httpx.Response(
                200, json={"resourceType": "Bundle", "type": "searchset", "link": [{"relation": "next", "url": url}]}
            )

    live.override = pages
    with pytest.raises(AcquisitionError, match="pagination_scope"):
        live.run()
    assert len(live.calls) == 3


@pytest.mark.parametrize(
    "status,code,requests",
    [(302, "redirect_refused", 1), (403, "forbidden", 1), (503, "http_5xx", 2), (401, "unauthorized", 2)],
)
def test_status_retries_and_renewal_are_bounded(live, status, code, requests):
    async def fail(request):
        if request.method == "GET":
            return httpx.Response(
                status, headers={"Location": "https://evil.invalid/secret", "Retry-After": "0"}, text=BODY
            )

    live.override = fail
    with pytest.raises(AcquisitionError, match=code):
        live.run()
    assert len([r for r in live.calls if r.method == "GET"]) == requests
    assert all(r.url.host == settings.EPIC_HOST for r in live.calls)


@pytest.mark.parametrize(
    "attack,code",
    [
        ("patient", "patient_scope"),
        ("nested", "patient_scope"),
        ("category", "resource_scope"),
        ("mapping", "mapping_rejected"),
    ],
)
def test_bad_resources_never_produce_a_candidate(live, attack, code):
    if attack == "patient":
        live.records[1]["subject"]["reference"] = f"Patient/{OTHER}"
    elif attack == "nested":
        live.records[1]["participant"] = [{"individual": {"reference": f"Patient/{OTHER}"}}]
    elif attack == "category":
        live.records[1]["resourceType"] = "MedicationRequest"

        # Force an out-of-profile resource in the Encounter search.
        async def malicious(request):
            if request.url.path.endswith("/Encounter"):
                return httpx.Response(
                    200, json={"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": live.records[1]}]}
                )

        live.override = malicious
        code = "resource_scope"
    else:
        del live.records[0]["birthDate"]
    adapter = live.adapter()
    with pytest.raises(AcquisitionError, match=code):
        asyncio.run(adapter.run())
    assert adapter.candidate is None and adapter.resources is None and adapter.token is None


@pytest.mark.parametrize(
    "severity,mode,resource_type,code",
    [
        ("warning", "outcome", "OperationOutcome", None),
        ("information", "outcome", "OperationOutcome", None),
        ("error", "outcome", "OperationOutcome", "invalid_bundle"),
        ("fatal", "outcome", "OperationOutcome", "invalid_bundle"),
        ("warning", "match", "OperationOutcome", "invalid_bundle"),
        ("warning", "outcome", "Condition", "invalid_bundle"),
    ],
)
def test_informational_search_outcome_is_an_empty_result_only(live, severity, mode, resource_type, code):
    """Epic answers an empty search with a warning-only outcome entry (for example 4101)."""
    secret = "invented-outcome-text-must-not-leak"

    async def empty_with_outcome(request):
        if request.method == "GET" and request.url.params.get("category") == "encounter-diagnosis":
            resource = {"resourceType": resource_type, "id": "invented-outcome"}
            if resource_type == "OperationOutcome":
                resource["issue"] = [
                    {
                        "severity": severity,
                        "code": "processing",
                        "details": {"coding": [{"code": "4101"}], "text": secret},
                    }
                ]
            return httpx.Response(
                200,
                json={
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": [{"search": {"mode": mode}, "resource": resource}],
                },
            )

    live.override = empty_with_outcome
    adapter = live.adapter()
    if code is not None:
        with pytest.raises(AcquisitionError, match=f"^{code}$"):
            asyncio.run(adapter.run())
        assert adapter.candidate is None and adapter.resources is None and adapter.token is None
        assert adapter.operations["Condition:encounter-diagnosis"]["execution"] == "failed"
        return
    asyncio.run(adapter.run())
    manifest, raw = adapter.candidate
    counts = manifest["coverage"]["operations"]["Condition:encounter-diagnosis"]
    assert (counts["execution"], counts["import"], counts["matches"], counts["unique"]) == (
        "succeeded",
        "returned_none",
        0,
        0,
    )
    assert len(live.calls) == 9
    assert adapter.outcome_codes == ["4101"]
    assert secret.encode() not in raw + encoded(manifest) and secret not in "".join(adapter.outcome_codes)


@pytest.mark.parametrize(
    "limits,code,maximum",
    [
        (Limits(requests_per_attempt=3), "request_limit", 3),
        (Limits(global_requests_per_hour=2), "outbound_quota", 2),
        (Limits(response_bytes=10), "response_too_large", 2),
        (Limits(attempt_bytes=10), "byte_limit", 1),
    ],
)
def test_request_and_byte_budgets(live, limits, code, maximum):
    with pytest.raises((AcquisitionError, RefreshRejected), match=code):
        live.run(limits=limits)
    assert len(live.calls) <= maximum


def test_stream_timeout_is_retried_once_and_responses_close(live):
    closed = []

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.1)
            yield b"{}"

        async def aclose(self):
            closed.append(True)

    async def slow(request):
        if request.method == "GET":
            return httpx.Response(200, headers={"content-type": "application/fhir+json"}, stream=Slow())

    live.override = slow
    with pytest.raises(AcquisitionError, match="timeout"):
        live.run(limits=replace(Limits(), per_request_s=0.01, retry_delay_s=0))
    assert len(closed) == 2 and len(live.calls) == 3


@pytest.mark.parametrize(
    "exception,code,count", [(httpx.ConnectError(BODY), "network", 2), (RuntimeError(BODY), "internal_error", 1)]
)
def test_transport_errors_are_sanitized(live, exception, code, count, caplog):
    async def fail(request):
        raise exception

    live.override = fail
    with pytest.raises(AcquisitionError) as error:
        live.run(limits=replace(Limits(), retry_delay_s=0))
    assert str(error.value) == code and len(live.calls) == count
    assert BODY not in caplog.text and PATIENT not in caplog.text and TOKEN not in caplog.text


def test_revocation_between_requests_stops_dispatch(live):
    async def revoke(request):
        if request.url.path.endswith(f"/Patient/{PATIENT}"):
            live.catalog.revoke(ALIAS)

    live.override = revoke
    with pytest.raises(AcquisitionError, match="authorization_revoked"):
        live.run()
    assert len(live.calls) == 2


def test_live_adapter_cannot_publish_even_with_mock_transport(live, tmp_path):
    from src.interop.epic.beta_generations import Generations

    adapter = live.run()
    service = Generations(live.catalog, tmp_path / "generations")
    try:
        with pytest.raises(AcquisitionError, match="live_publication_disabled"):
            asyncio.run(service.publish(adapter, ALIAS))
        assert list((tmp_path / "generations" / "staging").iterdir()) == []
        assert live.catalog.db.execute("SELECT COUNT(*) FROM generation").fetchone()[0] == 0
        assert (
            live.catalog.db.execute("SELECT active FROM patient_head WHERE alias = ?", (ALIAS,)).fetchone()[0] is None
        )
    finally:
        service.close()


@pytest.mark.parametrize(
    "mode,code", [("loop", "pagination_loop"), ("pages", "page_limit"), ("matches", "resource_limit")]
)
def test_pagination_and_match_limits(live, mode, code):
    async def pages(request):
        if request.url.path.endswith("/Encounter"):
            entry = {"resource": live.records[1]}
            initial = settings.FHIR_BASE + f"/Encounter?patient={PATIENT}"
            return httpx.Response(
                200,
                json={
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": [entry, entry] if mode == "matches" else [],
                    "link": [] if mode == "matches" else [{"relation": "next", "url": initial + "&page=2"}],
                },
            )

    live.override = pages
    limits = Limits(pages_per_search=1) if mode == "pages" else Limits(matches_per_search=1)
    with pytest.raises(AcquisitionError, match=code):
        live.run(limits=limits)
    assert len(live.calls) <= 4


@pytest.mark.parametrize(
    "body,headers,code",
    [
        (
            b'{"resourceType":"Patient","resourceType":"Patient"}',
            {"content-type": "application/fhir+json"},
            "invalid_json",
        ),
        (b"{}", {"content-type": "application/fhir+json", "content-encoding": "gzip"}, "unsupported_encoding"),
        (b"{}", {"content-type": "text/html"}, "unexpected_content_type"),
    ],
)
def test_invalid_fhir_payloads_are_rejected(live, body, headers, code):
    async def invalid(request):
        if request.method == "GET":
            # Explicit stream prevents the fixture library from pre-decoding gzip.
            return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))

    live.override = invalid
    with pytest.raises(AcquisitionError, match=code):
        live.run()
    assert len(live.calls) == 2


def test_retry_after_cannot_exceed_total_deadline(live):
    async def throttle(request):
        return httpx.Response(429, headers={"Retry-After": "120"})

    live.override = throttle
    with pytest.raises(AcquisitionError, match="throttled"):
        live.run()
    assert len(live.calls) == 1


def test_response_cleanup_has_the_same_time_budget(live):
    started = []

    class SlowClose(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"{}"

        async def aclose(self):
            started.append(True)
            await asyncio.sleep(0.1)

    async def slow_close(request):
        if request.method == "GET":
            return httpx.Response(200, headers={"content-type": "application/fhir+json"}, stream=SlowClose())

    live.override = slow_close
    with pytest.raises(AcquisitionError, match="timeout"):
        live.run(limits=Limits(per_request_s=0.01, retry_delay_s=0))
    assert len(started) == 2


def test_attempt_is_single_use_and_cancel_discards_candidate(live):
    adapter = live.run()
    with pytest.raises(AcquisitionError, match="attempt_reused"):
        asyncio.run(adapter.run())
    other = live.adapter()

    async def cancel(request):
        raise asyncio.CancelledError

    live.override = cancel
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(other.run())
    assert other.candidate is None and other.resources is None and other.token is None


def test_transport_cleanup_cannot_exceed_attempt_deadline(live, monkeypatch):
    class SlowCloseWire(httpx.MockTransport):
        async def aclose(self):
            await asyncio.sleep(0.2)

    wire = SlowCloseWire(live.respond)
    monkeypatch.setattr("src.interop.epic.beta_live_acquisition.sandbox_transport", lambda: wire)
    adapter = live.adapter(transport=None, limits=Limits(deadline_s=0.1))
    with pytest.raises(AcquisitionError, match="deadline"):
        asyncio.run(adapter.run())
    assert adapter.candidate is None and adapter.token is None and adapter.resources is None
