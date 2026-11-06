"""Read-only Epic sandbox FHIR client (MOCK/offline: ``httpx.MockTransport`` only, no Epic calls).

Ports the Phase 1 package scenarios and adds the gaps found in review: token refresh,
throttling, OperationOutcome entries, Binary scoping and long Epic ids.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from src.clinical.fhir_client import FHIRClientError, PatientScopeViolation
from src.interop.epic import settings
from src.interop.epic.auth import AccessToken
from src.interop.epic.client import EpicFHIRError, EpicPatientScopeError, EpicSandboxClient, pinned_url
from src.interop.epic.telemetry import EpicTelemetry

BASE = settings.FHIR_BASE
PID = "eTestPatient.Not-Real0001"
PATIENT = {"resourceType": "Patient", "id": PID, "gender": "female", "birthDate": "1987-09-12"}


def obs(oid: str, patient: str = PID, reference: str | None = None) -> dict:
    return {
        "resourceType": "Observation",
        "id": oid,
        "status": "final",
        "subject": {"reference": reference or f"Patient/{patient}"},
    }


def bundle(entries: list, next_url: str | None = None) -> dict:
    doc: dict = {"resourceType": "Bundle", "type": "searchset", "entry": entries}
    if next_url:
        doc["link"] = [{"relation": "next", "url": next_url}]
    return doc


def match(resource: dict) -> dict:
    return {"resource": resource, "search": {"mode": "match"}}


class FakeTokens:
    def __init__(self) -> None:
        self.telemetry = EpicTelemetry()
        self.issued = 0
        self._current: AccessToken | None = None

    def token(self) -> AccessToken:
        if self._current is None:
            self.issued += 1
            self._current = AccessToken(value=f"unit-test-token-{self.issued}", expires_at=9e12)
        return self._current

    def invalidate(self) -> None:
        self._current = None


def client(handler, **kwargs) -> tuple[EpicSandboxClient, FakeTokens, list[float]]:
    tokens = FakeTokens()
    sleeps: list[float] = []
    instance = EpicSandboxClient(
        tokens,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,  # type: ignore[arg-type]
        **kwargs,
    )
    return instance, tokens, sleeps


def fhir(payload: object, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(payload).encode(), headers={"content-type": "application/fhir+json", **headers}
    )


# --------------------------------------------------------------------------- Phase 1 package scenarios
def test_read_patient_uses_pinned_host_bearer_token_and_fhir_json():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.host == "fhir.epic.com"
        assert str(request.url) == f"{BASE}/Patient/{PID}"
        assert request.headers["authorization"] == "Bearer unit-test-token-1"
        assert request.headers["accept"] == "application/fhir+json"
        return fhir(PATIENT)

    c, _, _ = client(respond)
    assert c.read_patient(PID) == PATIENT


def test_patient_id_is_validated_before_any_request():
    c, tokens, _ = client(lambda _r: pytest.fail("network must not be reached"))
    for bad in ("../secret", "a/b", "", "x" * 300, "id with space"):
        with pytest.raises(ValueError):
            c.read_patient(bad)
    assert tokens.issued == 0


def test_wrong_patient_response_fails_closed():
    c, _, _ = client(lambda _r: fhir({**PATIENT, "id": "someone-else"}))
    with pytest.raises(EpicPatientScopeError) as info:
        c.read_patient(PID)
    assert isinstance(info.value, PatientScopeViolation)


def test_patient_scoped_pagination_follows_pinned_next_links():
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "page=2" in str(request.url):
            return fhir(bundle([match(obs("o2"))]))
        return fhir(bundle([match(obs("o1"))], f"{BASE}/Observation?patient={PID}&category=laboratory&page=2"))

    c, _, _ = client(respond)
    result = c.search("Observation", PID, category="laboratory")
    assert [r["id"] for r in result.resources] == ["o1", "o2"] and result.pages == 2
    assert seen[0] == f"{BASE}/Observation?patient={PID}&category=laboratory"


def test_cross_patient_resource_fails_closed():
    c, _, _ = client(lambda _r: fhir(bundle([match(obs("o1")), match(obs("o2", patient="another"))])))
    with pytest.raises(EpicPatientScopeError):
        c.search("Observation", PID, category="laboratory")
    assert c.telemetry.describe()["counters"]["patient_scope_rejection"] == 1


def test_cross_host_next_link_is_rejected_before_request():
    hosts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return fhir(bundle([], "https://evil.example/steal-token"))

    c, _, _ = client(respond)
    with pytest.raises(EpicFHIRError) as info:
        c.search("Encounter", PID)
    assert info.value.category == "url_outside_sandbox" and hosts == ["fhir.epic.com"]


def test_redirect_is_not_followed_and_token_never_appears_in_errors():
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example/"})

    c, _, _ = client(respond)
    with pytest.raises(EpicFHIRError) as info:
        c.read_patient(PID)
    assert info.value.category == "redirect_refused" and len(requests) == 1
    assert "unit-test-token" not in str(info.value) and PID not in str(info.value)


def test_forbidden_response_does_not_expose_body_or_token():
    c, _, _ = client(lambda _r: httpx.Response(403, text="private diagnostic and token"))
    with pytest.raises(EpicFHIRError) as info:
        c.read_patient(PID)
    assert info.value.category == "forbidden"
    assert "private diagnostic" not in str(info.value) and "unit-test-token" not in str(info.value)


def test_oversized_response_is_blocked():
    c, _, _ = client(lambda _r: fhir({"x": "y" * 3000}), max_response_bytes=1024)
    with pytest.raises(EpicFHIRError, match="response_too_large"):
        c.read_patient(PID)


def test_pagination_loop_and_page_limit_are_blocked():
    loop = f"{BASE}/Encounter?patient={PID}"
    c, _, _ = client(lambda _r: fhir(bundle([], loop)))
    with pytest.raises(EpicFHIRError, match="pagination_loop"):
        c.search("Encounter", PID)
    counter = iter(range(100))
    c, _, _ = client(lambda _r: fhir(bundle([], f"{BASE}/Encounter?patient={PID}&page={next(counter)}")), max_pages=3)
    with pytest.raises(EpicFHIRError, match="page_limit"):
        c.search("Encounter", PID)


def test_unsupported_types_and_categories_are_rejected_without_network():
    c, tokens, _ = client(lambda _r: pytest.fail("network not allowed"))
    for resource_type, category in (
        ("Patient", None),
        ("Procedure", None),
        ("Observation", "social-history"),
        ("Observation", None),
    ):
        with pytest.raises(ValueError):
            c.search(resource_type, PID, category=category)
    assert tokens.issued == 0


# --------------------------------------------------------------------------- review fixes
def test_operation_outcome_and_include_entries_are_recorded_not_ingested():
    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": "information",
                "code": "informational",
                "diagnostics": "free text that must not be kept",
                "details": {"coding": [{"code": "4101"}], "text": "Resource request returns no results."},
            }
        ],
    }
    page = bundle(
        [
            match(obs("o1")),
            {"resource": outcome, "search": {"mode": "outcome"}},
            {"resource": {"resourceType": "Encounter", "id": "e1"}, "search": {"mode": "include"}},
        ]
    )
    c, _, _ = client(lambda _r: fhir(page))
    result = c.search("Observation", PID, category="vital-signs")
    assert [r["id"] for r in result.resources] == ["o1"]
    assert result.outcomes == [("information", "4101")]
    assert "free text" not in json.dumps(result.summary())


def test_throttling_honours_bounded_retry_after():
    responses = iter([httpx.Response(429, headers={"retry-after": "120"}), fhir(PATIENT)])
    c, _, sleeps = client(lambda _r: next(responses), max_retry_after_s=5)
    assert c.read_patient(PID)["id"] == PID
    assert sleeps == [5]
    assert c.telemetry.describe()["counters"]["retry.429"] == 1


def test_persistent_server_errors_and_timeouts_stop_after_bounded_retries():
    calls: list[int] = []

    def failing(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    c, _, sleeps = client(failing, max_retries=2)
    with pytest.raises(EpicFHIRError, match="http_5xx"):
        c.read_patient(PID)
    assert len(calls) == 3 and sleeps == [0.5, 1.0]

    def slow(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    c, _, _ = client(slow, max_retries=1)
    with pytest.raises(EpicFHIRError, match="timeout"):
        c.read_patient(PID)


def test_expired_token_is_refreshed_once_then_fails_closed():
    responses = iter([httpx.Response(401), fhir(PATIENT)])
    c, tokens, _ = client(lambda _r: next(responses))
    assert c.read_patient(PID)["id"] == PID and tokens.issued == 2
    c, tokens, _ = client(lambda _r: httpx.Response(401))
    with pytest.raises(EpicFHIRError, match="unauthorized"):
        c.read_patient(PID)
    assert tokens.issued == 2


def test_non_json_content_type_is_rejected():
    c, _, _ = client(lambda _r: httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"}))
    with pytest.raises(EpicFHIRError, match="unexpected_content_type"):
        c.read_patient(PID)


def test_absolute_subject_reference_on_sandbox_base_only():
    same_base = obs("o1", reference=f"{BASE}/Patient/{PID}")
    c, _, _ = client(lambda _r: fhir(bundle([match(same_base)])))
    assert len(c.search("Observation", PID, category="laboratory").resources) == 1
    other = obs("o2", reference=f"https://other.example/fhir/Patient/{PID}")
    c, _, _ = client(lambda _r: fhir(bundle([match(other)])))
    with pytest.raises(EpicPatientScopeError):
        c.search("Observation", PID, category="laboratory")
    missing = {"resourceType": "Observation", "id": "o3"}
    c, _, _ = client(lambda _r: fhir(bundle([match(missing)])))
    with pytest.raises(EpicPatientScopeError):
        c.search("Observation", PID, category="laboratory")


def test_resource_limit_and_long_unconstrained_ids():
    long_id = "e" + "A1b2.C3-d4" * 12
    c, _, _ = client(lambda _r: fhir(bundle([match(obs(long_id)), match(obs("o2"))])))
    assert c.search("Observation", PID, category="laboratory").resources[0]["id"] == long_id
    c, _, _ = client(lambda _r: fhir(bundle([match(obs(f"o{i}")) for i in range(5)])), max_resources=3)
    with pytest.raises(EpicFHIRError, match="resource_limit"):
        c.search("Observation", PID, category="laboratory")


def test_binary_is_fetched_only_when_cited_by_a_verified_document():
    html = b"<html><body><p>Assessment: stable.</p></body></html>"

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/Binary/bNote1")
        return fhir({"resourceType": "Binary", "contentType": "text/html", "data": base64.b64encode(html).decode()})

    c, tokens, _ = client(respond)
    content = c.read_binary("Binary/bNote1", allowed={"Binary/bNote1"})
    assert content.content_type == "text/html" and content.data == html
    assert "Assessment" not in repr(content)
    absolute = c.read_binary(f"{BASE}/Binary/bNote1", allowed={"Binary/bNote1"})
    assert absolute.reference == "Binary/bNote1"
    issued = tokens.issued
    with pytest.raises(EpicPatientScopeError):
        c.read_binary("Binary/someoneElse", allowed={"Binary/bNote1"})
    assert tokens.issued == issued


@pytest.mark.parametrize(
    "url",
    [
        "http://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4/Patient/x",
        "https://fhir.epic.com:8443/interconnect-fhir-oauth/api/FHIR/R4/Patient/x",
        "https://user@fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4/Patient/x",
        "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4/../oauth2/token",
        "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4/%2e%2e/oauth2/token",
        "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4%2fPatient",
        "https://fhir.epic.com/other-path/api/FHIR/R4/Patient/x",
        "https://fhir.epic.com.evil.example/interconnect-fhir-oauth/api/FHIR/R4/Patient/x",
        "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4/Patient/x#frag",
    ],
)
def test_pinned_url_rejects_everything_outside_the_sandbox_base(url):
    with pytest.raises(EpicFHIRError):
        pinned_url(url)


def test_errors_are_client_errors_and_telemetry_holds_no_identifiers():
    c, _, _ = client(lambda _r: fhir(bundle([match(obs("o1"))])))
    c.search("Observation", PID, category="laboratory")
    rendered = json.dumps(c.telemetry.describe())
    assert PID not in rendered and "unit-test-token" not in rendered
    assert issubclass(EpicFHIRError, FHIRClientError)
