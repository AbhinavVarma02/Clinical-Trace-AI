"""Local MOCK runner (offline): invented sign-in, in-process Epic double with all eight profiles, local TLS pieces.

Everything runs in-process under the offline socket guard; nothing binds a port or reaches Epic.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import runpy
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from starlette.testclient import TestClient as StarletteClient

from api.beta_local import (
    BIND,
    HOST,
    IDP_HOST,
    PORT,
    InventedIdp,
    browser_command,
    build_local_app,
    epic_double,
    invented_resources,
    make_certificate,
    server_config,
)
from api.beta_oidc import AUDIENCE, CALLBACK, ISSUER
from src.interop.epic import settings
from tests.test_beta_browser_bootstrap import FORM, READ, assets  # noqa: F401 - `assets` is a fixture

ROOT = Path(__file__).resolve().parents[1]
ALPHA = "/beta/epic/patients/mock-alpha"
BRAVO = "/beta/epic/patients/mock-bravo"
ORIGIN = f"https://{HOST}"


@pytest.fixture
def client(tmp_path, assets):  # noqa: F811
    with TestClient(build_local_app(tmp_path, assets), base_url=ORIGIN, follow_redirects=False) as c:
        yield c


def sign_in(client) -> str:
    """The browser's sign-in: form POST, redirect to the invented IdP, redirect back. Returns the CSRF token."""
    start = client.post("/beta/auth/browser-login", headers=FORM)
    assert start.status_code == 303 and urlsplit(start.headers["location"]).netloc == IDP_HOST
    back = client.get(start.headers["location"])
    assert back.status_code == 302 and back.headers["location"].startswith(CALLBACK)
    done = client.get(back.headers["location"])
    assert done.status_code == 303 and done.headers["location"] == "/beta/app/"
    return client.get("/beta/auth/session", headers=READ).json()["csrf_token"]


def refresh(client, csrf, target, key="local-refresh-0001"):
    return client.post(target + "/refresh", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": key})


def settled(client, target):
    until = time.monotonic() + 10
    while time.monotonic() < until:
        view = client.get(target + "/record", headers=READ).json()
        if view["attempt"] and view["attempt"]["state"] not in {"queued", "running"}:
            return view
        time.sleep(0.02)
    pytest.fail("refresh did not finish")


# -- the whole local flow -------------------------------------------------------------------------------------


def test_sign_in_then_refresh_publishes_both_invented_patients_with_all_eight_profiles(client):
    csrf = sign_in(client)
    assert client.get(ALPHA + "/record", headers=READ).json()["record"] is None  # nothing before a refresh

    assert refresh(client, csrf, ALPHA).status_code == 202
    alpha = settled(client, ALPHA)
    assert alpha["attempt"]["state"] == "succeeded" and alpha["serving"] == "fresh"
    record = alpha["record"]
    assert {
        name: record[name]["total"] for name in ("conditions", "encounters", "labs", "vitals", "reports", "notes")
    } == {
        "conditions": 3,
        "encounters": 2,
        "labs": 4,
        "vitals": 3,
        "reports": 1,
        "notes": 1,
    }
    operations = alpha["coverage"]["operations"]
    assert {name: op["import"] for name, op in operations.items()} == {
        "Patient": "imported",
        "Encounter": "imported",
        "Condition:problem-list-item": "imported",
        "Condition:encounter-diagnosis": "imported",
        "Observation:laboratory": "imported",
        "Observation:vital-signs": "imported",
        "DiagnosticReport": "imported",
        "DocumentReference:clinical-note": "not_imported",  # metadata only, by policy
        "MedicationRequest": "not_imported",
        "Binary": "not_imported",
    }
    assert alpha["coverage"]["sources"]["DocumentReference.discharge_summary_metadata"] == "returned"
    assert record["notes"]["items"][0]["type"] == "Discharge summary"
    assert [lab["value"] for lab in record["labs"]["items"] if lab["name"] == "Creatinine"] == [1.0, 1.2, 1.1]

    assert refresh(client, csrf, BRAVO, key="local-refresh-0002").status_code == 202
    bravo = settled(client, BRAVO)
    assert bravo["attempt"]["state"] == "succeeded"
    assert bravo["record"]["notes"]["total"] == 0 and bravo["record"]["reports"]["total"] == 0
    assert bravo["coverage"]["sources"]["DocumentReference.metadata"] == "returned_none"
    assert bravo["coverage"]["operations"]["Condition:encounter-diagnosis"]["import"] == "returned_none"
    assert {lab["name"]: lab["value"] for lab in bravo["record"]["labs"]["items"]} == {
        "Creatinine": 4.8,
        "Potassium": 5.4,
    }
    # isolation: nothing of Alpha reached Bravo's record, and the other way round
    assert "Alpha" not in str(bravo["record"]) and "Bravo" not in str(alpha["record"])


def test_a_second_refresh_is_held_by_the_existing_cooldown(client):
    csrf = sign_in(client)
    assert refresh(client, csrf, ALPHA).status_code == 202
    settled(client, ALPHA)
    again = refresh(client, csrf, ALPHA, key="local-refresh-0003")
    assert again.status_code == 429 and again.json() == {"error": {"code": "quota_exhausted"}}
    assert int(again.headers["retry-after"]) > 0


def test_refresh_still_needs_origin_csrf_and_an_idempotency_key(client):
    csrf = sign_in(client)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": "local-refresh-0004"}
    for missing, status, code in (
        ("Origin", 403, "invalid_origin"),
        ("X-CSRF-Token", 403, "csrf_rejected"),
        ("Idempotency-Key", 400, "idempotency_key_required"),
    ):
        response = client.post(ALPHA + "/refresh", headers={k: v for k, v in headers.items() if k != missing})
        assert (response.status_code, response.json()["error"]["code"]) == (status, code)


def test_the_local_app_exposes_no_live_alias_and_no_clinical_run_route(client):
    csrf = sign_in(client)
    assert client.get("/beta/epic/patients/live-synthetic-1/record", headers=READ).status_code == 404
    assert client.post(
        ALPHA + "/generations/" + "0" * 32 + "/runs",
        json={"question": "x"},
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    ).status_code in {404, 409}
    assert client.post(ALPHA + "/candidate/erase", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code == 404


# -- the invented identity provider ---------------------------------------------------------------------------


def challenge_for(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def good_query(**overrides):
    base = {
        "response_type": "code",
        "client_id": AUDIENCE,
        "redirect_uri": CALLBACK,
        "code_challenge_method": "S256",
        "code_challenge": challenge_for("v" * 43),
        "state": "s" * 43,
        "nonce": "n" * 43,
    }
    return {k: v for k, v in (base | overrides).items() if v is not None}


@pytest.mark.parametrize(
    "change",
    [
        {"client_id": "someone-else"},
        {"redirect_uri": "https://evil.invalid/callback"},
        {"response_type": "token"},
        {"code_challenge_method": "plain"},
        {"code_challenge": None},
        {"state": "short"},
        {"nonce": None},
    ],
    ids=["client", "redirect", "response_type", "plain_pkce", "no_challenge", "bad_state", "no_nonce"],
)
def test_the_idp_enforces_client_redirect_and_pkce(change):
    idp = InventedIdp(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    with StarletteClient(idp.app(), base_url=f"https://{IDP_HOST}", follow_redirects=False) as c:
        assert c.get("/authorize", params=good_query(**change)).status_code == 400
    assert idp.codes == {}


def test_an_idp_code_is_single_use_and_bound_to_its_pkce_verifier():
    idp = InventedIdp(rsa.generate_private_key(public_exponent=65537, key_size=2048))

    def exchange(code, verifier):
        return idp.token(
            httpx.Request(
                "POST",
                f"{ISSUER}/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": AUDIENCE,
                    "redirect_uri": CALLBACK,
                    "code": code,
                    "code_verifier": verifier,
                },
            )
        )

    with StarletteClient(idp.app(), base_url=f"https://{IDP_HOST}", follow_redirects=False) as c:

        def issue():
            location = c.get("/authorize", params=good_query()).headers["location"]
            return parse_qs(urlsplit(location).query)["code"][0]

        assert exchange(issue(), "w" * 43).status_code == 400  # wrong verifier burns the code
        code = issue()
        assert exchange(code, "v" * 43).status_code == 200
        assert exchange(code, "v" * 43).status_code == 400  # replay


def test_hosts_are_dispatched_strictly_and_the_idp_is_not_reachable_through_the_app_host(client):
    assert client.get(f"https://{IDP_HOST}/beta/app/").status_code == 404  # the IdP serves /authorize only
    assert client.get(f"https://{IDP_HOST}/beta/epic/patients/mock-alpha/record", headers=READ).status_code == 404
    through_app = client.get(f"{ORIGIN}/authorize", params=good_query())
    assert through_app.status_code == 401 and "location" not in through_app.headers  # the owner boundary answers


# -- the Epic double --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_",
    [
        httpx.Request("GET", "https://evil.invalid/api/FHIR/R4/Patient/invented-patient-alpha"),
        httpx.Request("POST", f"https://{httpx.URL(settings.TOKEN_URL).host}{settings.FHIR_BASE_PATH}/Observation"),
        httpx.Request("GET", f"{settings.FHIR_BASE}/Patient/not-an-invented-patient"),
        httpx.Request("GET", f"{settings.FHIR_BASE}/Observation?patient=not-an-invented-patient"),
        httpx.Request("GET", f"{settings.FHIR_BASE}/Observation"),
    ],
    ids=["other_host", "post", "unknown_patient_read", "unknown_patient_search", "no_patient"],
)
def test_the_epic_double_answers_only_the_invented_epic_urls(request_):
    import asyncio

    assert asyncio.run(epic_double(request_)).status_code == 404


def test_the_invented_patients_are_synthetic_and_never_carry_attachment_bodies():
    for patient in ("invented-patient-alpha", "invented-patient-bravo"):
        resources = invented_resources(patient)
        assert resources[0]["resourceType"] == "Patient" and "(synthetic)" in resources[0]["name"][0]["text"]
        assert all(r["subject"]["reference"] == f"Patient/{patient}" for r in resources[1:] if "subject" in r)
        assert "data" not in str(resources)  # a DocumentReference attachment carries only a url, never content
    with pytest.raises(KeyError):
        invented_resources("someone-real")


# -- local TLS and the browser command ------------------------------------------------------------------------


def test_the_certificate_is_a_short_lived_leaf_for_exactly_the_two_invented_names(tmp_path):
    certificate = make_certificate(tmp_path)
    parsed = x509.load_pem_x509_certificate(certificate.cert_path.read_bytes())
    names = parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
        x509.DNSName
    )
    assert sorted(names) == sorted([HOST, IDP_HOST])
    assert parsed.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert parsed.not_valid_after_utc - parsed.not_valid_before_utc <= datetime.timedelta(days=2)
    spki = parsed.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert certificate.spki_sha256 == base64.b64encode(hashlib.sha256(spki).digest()).decode()
    assert certificate.key_path.is_file()


def test_the_server_binds_loopback_only_on_the_default_https_port(tmp_path, assets):  # noqa: F811
    certificate = make_certificate(tmp_path)
    config = server_config(build_local_app(tmp_path / "state", assets), certificate)
    assert (config.host, config.port) == (BIND, PORT) == ("127.0.0.1", 443)
    assert config.ssl_certfile and config.proxy_headers is False


def test_the_browser_command_maps_two_names_and_trusts_only_this_certificate(tmp_path):
    certificate = make_certificate(tmp_path)
    command = browser_command("chrome", tmp_path / "profile", certificate)
    assert f"--host-resolver-rules=MAP {HOST} 127.0.0.1, MAP {IDP_HOST} 127.0.0.1" in command
    assert f"--ignore-certificate-errors-spki-list={certificate.spki_sha256}" in command
    assert command[-1] == f"https://{HOST}/beta/app/"
    assert not any(
        "disable-web-security" in c or "no-sandbox" in c or c == "--ignore-certificate-errors" for c in command
    )


# -- containment ------------------------------------------------------------------------------------------------


def test_nothing_deployable_imports_the_local_runner():
    importers = [
        path.relative_to(ROOT).as_posix()
        for folder in ("api", "src", "deploy", "dashboard")
        for path in (ROOT / folder).rglob("*.py")
        if "beta_local" in path.read_text(encoding="utf-8", errors="ignore") and path.name != "beta_local.py"
    ]
    assert importers == []
    assert "beta_local" not in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_the_launcher_refuses_to_start_without_a_built_frontend(tmp_path):
    result = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts" / "run_beta_local.py"), "--assets", str(tmp_path / "missing")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2 and "vite build --config vite.beta.config.ts" in result.stdout


def test_runtime_initialization_refuses_a_worker_thread_before_importing(monkeypatch):
    from api import beta_local

    monkeypatch.setattr(beta_local, "import_module", lambda name: pytest.fail("worker attempted dependency import"))
    errors = []

    def initialize():
        try:
            beta_local.initialize_runtime_dependencies()
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=initialize)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    assert "main thread" in str(errors[0])


@pytest.mark.parametrize("failed_module", ["numpy", "src.runtime"])
def test_the_launcher_fails_closed_before_creating_state_or_serving(monkeypatch, assets, capsys, failed_module):  # noqa: F811
    from api import beta_local

    launcher = runpy.run_path(str(ROOT / "scripts" / "run_beta_local.py"))
    monkeypatch.setattr(sys.stdout, "reconfigure", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(sys, "argv", ["run_beta_local.py", "--assets", str(assets)])

    def load(name):
        if name == failed_module:
            raise ImportError("invented sensitive exception detail")
        return object()

    monkeypatch.setattr(beta_local, "import_module", load)
    monkeypatch.setattr(beta_local, "build_local_app", lambda *args: pytest.fail("partially initialized app"))
    monkeypatch.setattr(launcher["tempfile"], "mkdtemp", lambda **kwargs: pytest.fail("state before readiness"))
    assert launcher["main"]() == 1
    output = capsys.readouterr().out
    assert "initialization failed (ImportError); server not started" in output
    assert "dependencies ready" not in output and "sensitive exception detail" not in output


def test_the_launcher_initializes_on_main_thread_before_constructing_or_serving(monkeypatch, tmp_path, assets, capsys):  # noqa: F811
    from api import beta_local

    launcher = runpy.run_path(str(ROOT / "scripts" / "run_beta_local.py"))
    scope = launcher["main"].__globals__
    monkeypatch.setattr(sys.stdout, "reconfigure", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(sys, "argv", ["run_beta_local.py", "--assets", str(assets)])
    events = []
    preserved = set(tmp_path.iterdir())
    make_temp = launcher["tempfile"].mkdtemp
    monkeypatch.setattr(launcher["tempfile"], "mkdtemp", lambda **kwargs: make_temp(dir=tmp_path, **kwargs))

    def initialize():
        assert threading.current_thread() is threading.main_thread()
        events.append("initialized")
        return 0.125

    def build(data, assets):
        assert events == ["initialized"]
        events.append("constructed")
        return object()

    async def serve(app, certificate):
        assert events == ["initialized", "constructed"]
        assert "dependencies ready in 0.125 s" in capsys.readouterr().out
        events.append("served")

    monkeypatch.setattr(beta_local, "initialize_runtime_dependencies", initialize)
    monkeypatch.setattr(beta_local, "build_local_app", build)
    monkeypatch.setitem(scope, "find_browser", lambda: None)
    monkeypatch.setitem(scope, "serve", serve)
    assert launcher["main"]() == 0
    assert events == ["initialized", "constructed", "served"]
    assert set(tmp_path.iterdir()) == preserved  # default state, certificate and profile all cleaned up


# -- questions, revoke and sign-out as the browser performs them ------------------------------------------------


def ask(client, csrf, alias, gid, question, **extra_headers):
    return client.post(
        f"/beta/epic/patients/{alias}/generations/{gid}/runs",
        json=question if isinstance(question, dict) else {"question": question},
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf, **extra_headers},
    )


def published(client, csrf, target=ALPHA, key="local-refresh-0001"):
    assert refresh(client, csrf, target, key=key).status_code == 202
    view = settled(client, target)
    assert view["attempt"]["state"] == "succeeded"
    return view


def test_a_question_is_answered_from_the_active_generation_with_citations_that_resolve(client):
    csrf = sign_in(client)
    view = published(client, csrf)
    gid = view["active"]["generation_id"]
    response = ask(client, csrf, "mock-alpha", gid, "What is the latest creatinine?")
    assert response.status_code == 200
    answer = response.json()
    assert answer["outcome"] == "answered" and answer["serving"] == "fresh"
    assert answer["verification"]["status"] == "verified"
    assert answer["verification"]["claims_supported"] == answer["verification"]["claims_total"] > 0
    evidence = {item["evidence_id"]: item for item in answer["evidence"]}
    citations = [c for claim in answer["answer"]["claims"] for c in claim["citations"]]
    assert citations and all(c in evidence for c in citations)  # nothing cited that was not returned
    record = view["record"]
    known = {
        item["source"]["reference"]
        for name in ("labs", "vitals", "conditions", "encounters", "reports")
        for item in record[name]["items"]
    }
    assert {evidence[c]["source_ref"] for c in citations} <= known  # every cited source is in the shown record
    assert {item["evidence_context"]["generation_id"] for item in answer["evidence"]} == {gid}
    assert answer["review"]["required"] is True and answer["inference"]["mode"] == "deterministic"
    assert answer["answer"]["claims"][0]["text"].startswith("Creatinine")


def test_a_treatment_question_is_refused_without_claims_or_evidence(client):
    csrf = sign_in(client)
    gid = published(client, csrf)["active"]["generation_id"]
    answer = ask(client, csrf, "mock-alpha", gid, "Should I start an antibiotic?").json()
    assert answer["outcome"] == "refused" and answer["refusal"]["category"] == "clinical_decision"
    assert answer["answer"]["claims"] == [] and answer["evidence"] == [] and answer["verification"] is None


def test_a_question_cannot_reach_another_patient_and_each_answer_stays_with_its_patient(client):
    csrf = sign_in(client)
    alpha = published(client, csrf)["active"]["generation_id"]
    bravo = published(client, csrf, BRAVO, key="local-refresh-0002")["active"]["generation_id"]
    crossed = ask(client, csrf, "mock-alpha", alpha, "Compare this with mock-bravo")
    assert (crossed.status_code, crossed.json()["error"]["code"]) == (403, "patient_scope")
    assert (
        ask(client, csrf, "mock-alpha", bravo, "What is the latest creatinine?").status_code == 404
    )  # wrong generation
    answer = ask(client, csrf, "mock-bravo", bravo, "What is the latest creatinine?").json()
    assert "Alpha" not in str(answer) and "4.8" in str(answer)


@pytest.mark.parametrize(
    "body",
    [{"question": ""}, {"question": "x" * 1001}, {"question": "q", "extra": 1}, {"question": 5}, {}],
    ids=["empty", "overlong", "extra_field", "not_a_string", "no_question"],
)
def test_the_question_body_rules_the_client_relies_on(client, body):
    csrf = sign_in(client)
    gid = published(client, csrf)["active"]["generation_id"]
    response = ask(client, csrf, "mock-alpha", gid, body)
    assert (response.status_code, response.json()["error"]["code"]) == (400, "invalid_question")


def test_a_stale_generation_needs_an_explicit_acknowledgement_before_it_answers(tmp_path, assets):  # noqa: F811
    now = [time.time()]
    with TestClient(
        build_local_app(tmp_path, assets, clock=lambda: now[0]), base_url=ORIGIN, follow_redirects=False
    ) as c:
        csrf = sign_in(c)
        gid = published(c, csrf)["active"]["generation_id"]
        now[0] += 25 * 3600  # past the 24 h freshness window; the owner session has also lapsed
        csrf = sign_in(c)
        assert c.get(ALPHA + "/record", headers=READ).json()["serving"] == "stale"
        blocked = ask(c, csrf, "mock-alpha", gid, "What is the latest creatinine?")
        assert (blocked.status_code, blocked.json()["error"]["code"]) == (409, "cache_acknowledgement_required")
        allowed = ask(c, csrf, "mock-alpha", gid, "What is the latest creatinine?", **{"X-Beta-Cache-Ack": "1"})
        assert allowed.status_code == 200 and allowed.json()["serving"] == "stale"


def test_revoking_a_patient_removes_it_everywhere_the_browser_looks(client):
    csrf = sign_in(client)
    gid = published(client, csrf)["active"]["generation_id"]
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    assert client.post(ALPHA + "/revoke", headers=headers).status_code == 204
    assert client.get(ALPHA + "/record", headers=READ).status_code == 404
    assert [p["alias"] for p in client.get("/beta/epic/patients", headers=READ).json()["patients"]] == ["mock-bravo"]
    assert ask(client, csrf, "mock-alpha", gid, "What is the latest creatinine?").status_code == 404
    assert refresh(client, csrf, ALPHA, key="local-refresh-0009").status_code == 404
    assert client.get(BRAVO + "/record", headers=READ).status_code == 200  # the other patient is untouched


def test_signing_out_ends_the_session_and_the_old_cookie_is_dead(client):
    csrf = sign_in(client)
    token = client.cookies.get("__Host-beta-session")
    assert client.post("/beta/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code == 204
    assert client.get("/beta/auth/session", headers=READ).status_code == 401
    replay = client.get("/beta/auth/session", headers={**READ, "Cookie": f"__Host-beta-session={token}"})
    assert replay.status_code == 401
    assert client.get(ALPHA + "/record", headers={**READ, "Cookie": f"__Host-beta-session={token}"}).status_code == 401
    assert client.post("/beta/auth/logout", headers={"Origin": ORIGIN}).status_code == 401  # nothing to end
