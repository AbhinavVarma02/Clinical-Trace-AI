"""LOCAL-ONLY, MOCK-ONLY runner pieces for the Epic Beta browser experience.

This module exists so the owner can use the Beta browser UI on their own machine with synthetic data. It is never
imported by the Stable API, never reachable from a deployment, and it must stay that way:

* The "identity provider" below authenticates NOBODY. It redirects back to the app so a local browser can complete the
  invented sign-in flow (PKCE is still verified). It is acceptable only because the server binds to 127.0.0.1, serves
  invented MOCK patients, and talks to an in-process Epic double. It is not genuine owner authentication (AUTH-2).
* The Epic "server" is an in-process ``httpx.MockTransport`` serving invented FHIR resources for the eight approved
  operation profiles. No Epic request and no network request of any kind is made.
* LIVE promotion, LIVE data in the browser and clinical questions stay disabled: the app is built exactly as the tests
  build it (``create_mock_beta_app`` without ``live``).
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode

import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from api.beta import create_mock_beta_app
from api.beta_oidc import AUDIENCE, CALLBACK, CODE_SHAPE, ISSUER, SUBJECT, TOKEN_SHAPE
from src.interop.epic import settings
from src.interop.epic.beta_catalog import MOCK_PATIENTS

HOST = "beta.invalid"
IDP_HOST = "owner-idp.invalid"
PORT = 443  # the app's origin is https://beta.invalid with no port, so the server must listen on the default port
BIND = "127.0.0.1"  # not configurable: this runner is local-only
META = {"versionId": "upstream-1", "lastUpdated": "2026-10-30T09:00:00Z"}
NOTE_SYSTEM = "http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category"


# -- invented patients: the eight approved operation profiles -------------------------------------------------------


def _concept(system: str, code: str, display: str | None = None) -> dict[str, Any]:
    coding: dict[str, Any] = {"system": system, "code": code}
    if display:
        coding["display"] = display
    return {"coding": [coding]}


def _condition(patient: str, rid: str, category: str, code: str, text: str, onset: str) -> dict[str, Any]:
    return {
        "resourceType": "Condition",
        "id": rid,
        "subject": {"reference": f"Patient/{patient}"},
        "category": [_concept("http://terminology.hl7.org/CodeSystem/condition-category", category)],
        "clinicalStatus": _concept("http://terminology.hl7.org/CodeSystem/condition-clinical", "active"),
        "code": {
            "coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": code, "display": text}],
            "text": text,
        },
        "onsetDateTime": onset,
        "meta": META,
    }


def _observation(
    patient: str, rid: str, category: str, loinc: str, name: str, value: float, unit: str, when: str
) -> dict[str, Any]:
    return {
        "resourceType": "Observation",
        "id": rid,
        "status": "final",
        "subject": {"reference": f"Patient/{patient}"},
        "category": [_concept("http://terminology.hl7.org/CodeSystem/observation-category", category)],
        "code": {"coding": [{"system": "http://loinc.org", "code": loinc, "display": name}], "text": name},
        "effectiveDateTime": when,
        "valueQuantity": {"value": value, "unit": unit},
        "meta": META,
    }


def _encounter(patient: str, rid: str, cls: str, display: str, start: str, end: str, reason: str) -> dict[str, Any]:
    return {
        "resourceType": "Encounter",
        "id": rid,
        "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": cls, "display": display},
        "period": {"start": start, "end": end},
        "reasonCode": [{"text": reason}],
        "subject": {"reference": f"Patient/{patient}"},
        "meta": META,
    }


def invented_resources(patient: str) -> list[dict[str, Any]]:
    """Synthetic FHIR resources for one invented patient id. Nothing here is, or is derived from, real data."""
    subject = {"reference": f"Patient/{patient}"}
    if patient == MOCK_PATIENTS["mock-alpha"]:
        resources = [
            {
                "resourceType": "Patient",
                "id": patient,
                "gender": "female",
                "birthDate": "1962-04-09",
                "name": [{"given": ["Mock"], "family": "Alpha", "text": "Mock Alpha (synthetic)"}],
                "meta": META,
            },
            _encounter(
                patient,
                "a-enc-1",
                "IMP",
                "inpatient encounter",
                "2026-10-12T14:00:00Z",
                "2026-10-15T11:00:00Z",
                "Cough and fever",
            ),
            _encounter(
                patient,
                "a-enc-2",
                "AMB",
                "ambulatory",
                "2026-10-28T15:00:00Z",
                "2026-10-28T15:30:00Z",
                "Post-discharge follow-up",
            ),
            _condition(
                patient, "a-cond-1", "problem-list-item", "I10", "Essential hypertension", "2025-03-02T00:00:00Z"
            ),
            _condition(
                patient, "a-cond-2", "problem-list-item", "E11.9", "Type 2 diabetes mellitus", "2023-06-14T00:00:00Z"
            ),
            _condition(patient, "a-cond-3", "encounter-diagnosis", "J20.9", "Acute bronchitis", "2026-10-12T14:00:00Z"),
            _observation(
                patient, "a-lab-1", "laboratory", "2160-0", "Creatinine", 1.0, "mg/dL", "2026-10-12T16:00:00Z"
            ),
            _observation(
                patient, "a-lab-2", "laboratory", "2160-0", "Creatinine", 1.2, "mg/dL", "2026-10-13T06:00:00Z"
            ),
            _observation(
                patient, "a-lab-3", "laboratory", "2160-0", "Creatinine", 1.1, "mg/dL", "2026-10-15T06:00:00Z"
            ),
            _observation(
                patient, "a-lab-4", "laboratory", "2823-3", "Potassium", 4.1, "mmol/L", "2026-10-13T06:00:00Z"
            ),
            _observation(patient, "a-vit-1", "vital-signs", "8867-4", "Heart rate", 96, "/min", "2026-10-12T14:30:00Z"),
            _observation(patient, "a-vit-2", "vital-signs", "8867-4", "Heart rate", 78, "/min", "2026-10-15T08:00:00Z"),
            _observation(
                patient, "a-vit-3", "vital-signs", "59408-5", "Oxygen saturation", 94, "%", "2026-10-12T14:30:00Z"
            ),
            {
                "resourceType": "DiagnosticReport",
                "id": "a-dxr-1",
                "status": "final",
                "subject": subject,
                "code": {"text": "Chest X-ray, 2 views (synthetic)"},
                "effectiveDateTime": "2026-10-12T17:00:00Z",
                "conclusion": "No focal consolidation. Mild bronchial wall thickening.",
                "result": [{"reference": "Observation/a-lab-1"}],
                "meta": META,
            },
            {
                "resourceType": "DocumentReference",
                "id": "a-doc-1",
                "status": "current",
                "subject": subject,
                "category": [_concept(NOTE_SYSTEM, "clinical-note")],
                "type": {
                    "coding": [{"system": "http://loinc.org", "code": "18842-5", "display": "Discharge summary"}],
                    "text": "Discharge summary",
                },
                "description": "Discharge summary",
                "date": "2026-10-15T12:00:00Z",
                "content": [{"attachment": {"contentType": "text/plain", "url": "Binary/invented-note-1"}}],
                "meta": META,
            },
        ]
    elif patient == MOCK_PATIENTS["mock-bravo"]:
        resources = [
            {
                "resourceType": "Patient",
                "id": patient,
                "gender": "male",
                "birthDate": "1951-11-30",
                "name": [{"given": ["Mock"], "family": "Bravo", "text": "Mock Bravo (synthetic)"}],
                "meta": META,
            },
            _encounter(
                patient,
                "b-enc-1",
                "EMER",
                "emergency",
                "2026-10-20T03:00:00Z",
                "2026-10-20T11:00:00Z",
                "Weakness and nausea",
            ),
            _condition(
                patient,
                "b-cond-1",
                "problem-list-item",
                "N18.4",
                "Chronic kidney disease, stage 4",
                "2024-08-19T00:00:00Z",
            ),
            _observation(
                patient, "b-lab-1", "laboratory", "2160-0", "Creatinine", 4.8, "mg/dL", "2026-10-20T04:00:00Z"
            ),
            _observation(
                patient, "b-lab-2", "laboratory", "2823-3", "Potassium", 5.4, "mmol/L", "2026-10-20T04:00:00Z"
            ),
            _observation(
                patient, "b-vit-1", "vital-signs", "8867-4", "Heart rate", 104, "/min", "2026-10-20T03:10:00Z"
            ),
        ]
    else:
        raise KeyError(patient)
    return resources


def _bundle(resources: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [{"resource": resource} for resource in resources],
            "link": [],
        },
    )


async def epic_double(request: httpx.Request) -> httpx.Response:
    """In-process stand-in for Epic's token endpoint and FHIR searches. Anything unexpected is an error, never a fetch."""
    if str(request.url) == settings.TOKEN_URL and request.method == "POST":
        return httpx.Response(
            200, json={"access_token": "invented-fhir-bearer", "token_type": "Bearer", "expires_in": 300}
        )
    prefix = settings.FHIR_BASE_PATH + "/"
    if (
        request.method != "GET"
        or request.url.host != httpx.URL(settings.TOKEN_URL).host
        or not request.url.path.startswith(prefix)
    ):
        return httpx.Response(404)
    path = request.url.path[len(prefix) :]
    try:
        if path.startswith("Patient/"):
            return httpx.Response(200, json=invented_resources(path.split("/", 1)[1])[0])
        patient = request.url.params["patient"]
        resources = [r for r in invented_resources(patient) if r["resourceType"] == path]
    except (KeyError, ValueError):
        return httpx.Response(404)
    category = request.url.params.get("category")
    if category:
        resources = [
            r for r in resources if any(c["code"] == category for i in r.get("category", []) for c in i["coding"])
        ]
    return _bundle(resources)


# -- invented identity provider (authenticates nobody; local MOCK only) ----------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class InventedIdp:
    """Issues an id_token for the single invented owner to whoever completes the redirect. PKCE and ``redirect_uri``
    are enforced so the relying party's own checks are exercised; there is no credential check by design."""

    def __init__(self, key: rsa.RSAPrivateKey, clock: Callable[[], float] = time.time) -> None:
        self.key, self.clock = key, clock
        self.codes: dict[str, dict[str, str]] = {}

    async def authorize(self, request: Request) -> Response:
        query = request.query_params
        challenge = query.get("code_challenge", "")
        state, nonce = query.get("state", ""), query.get("nonce", "")
        if (
            query.get("response_type") != "code"
            or query.get("client_id") != AUDIENCE
            or query.get("redirect_uri") != CALLBACK
            or query.get("code_challenge_method") != "S256"
            or not TOKEN_SHAPE.fullmatch(state)
            or not TOKEN_SHAPE.fullmatch(nonce)
            or not TOKEN_SHAPE.fullmatch(challenge)
        ):
            return PlainTextResponse("invalid_request", status_code=400)
        if len(self.codes) >= 32:
            self.codes.pop(next(iter(self.codes)))
        code = secrets.token_urlsafe(24)
        self.codes[code] = {"nonce": nonce, "challenge": challenge}
        return RedirectResponse(f"{CALLBACK}?{urlencode({'state': state, 'code': code})}", status_code=302)

    def token(self, request: httpx.Request) -> httpx.Response:
        fields = {key: values[0] for key, values in parse_qs(request.content.decode()).items() if len(values) == 1}
        grant = self.codes.pop(fields.get("code", ""), None)  # single use
        if (
            request.url != httpx.URL(f"{ISSUER}/token")
            or grant is None
            or not CODE_SHAPE.fullmatch(fields.get("code", ""))
            or fields.get("grant_type") != "authorization_code"
            or fields.get("client_id") != AUDIENCE
            or fields.get("redirect_uri") != CALLBACK
            or _b64(hashlib.sha256(fields.get("code_verifier", "").encode()).digest()) != grant["challenge"]
        ):
            return httpx.Response(400, json={"error": "invalid_grant"})
        now = int(self.clock())
        claims = {"iss": ISSUER, "sub": SUBJECT, "aud": AUDIENCE, "nonce": grant["nonce"], "iat": now, "exp": now + 60}
        head = _b64(b'{"alg":"RS256","typ":"JWT"}')
        body = _b64(json.dumps(claims, separators=(",", ":")).encode())
        signature = self.key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return httpx.Response(200, json={"id_token": f"{head}.{body}.{_b64(signature)}"})

    def app(self) -> Starlette:
        return Starlette(routes=[Route("/authorize", self.authorize)])


# -- the assembled local app -----------------------------------------------------------------------------------------


def initialize_runtime_dependencies() -> float:
    """Finish cached, process-wide imports before the local launcher starts serving.

    Construct no runtime, database or provider here: generation preparation still owns those in its worker.
    Import failures propagate so the launcher cannot serve a partially initialized app.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("local runtime dependencies must initialize on the main thread")
    started = time.perf_counter()
    import_module("numpy")
    import_module("src.runtime")
    return time.perf_counter() - started


def build_local_app(data_dir: Path, assets: Path, *, clock: Callable[[], float] = time.time) -> ASGIApp:
    """The real MOCK Beta app plus the invented IdP, dispatched by Host header. MOCK only: no ``live`` argument."""
    idp = InventedIdp(rsa.generate_private_key(public_exponent=65537, key_size=2048), clock)
    epic_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    beta = create_mock_beta_app(
        data_dir / "control.sqlite",
        oidc_key=idp.key.public_key(),
        epic_key=epic_key,
        oidc_transport=httpx.MockTransport(idp.token),
        epic_transport=httpx.MockTransport(epic_double),
        generation_root=data_dir / "generations",
        browser_assets_root=assets,
        clock=clock,
    )
    idp_app = idp.app()

    async def router(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and dict(scope["headers"]).get(b"host", b"").decode() == IDP_HOST:
            await idp_app(scope, receive, send)
        else:
            await beta(scope, receive, send)  # lifespan and every other host go to the Beta app, which checks its host

    return router


# -- local TLS -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Certificate:
    cert_path: Path
    key_path: Path
    spki_sha256: str  # base64, for Chromium's --ignore-certificate-errors-spki-list


def make_certificate(directory: Path, *, valid_for: datetime.timedelta = datetime.timedelta(days=1)) -> Certificate:
    """A throwaway self-signed certificate for the two invented names. It is not installed or trusted anywhere."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + valid_for)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST), x509.DNSName(IDP_HOST)]), False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "local-cert.pem", directory / "local-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    spki = certificate.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return Certificate(cert_path, key_path, base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii"))


def server_config(app: ASGIApp, certificate: Certificate) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        host=BIND,
        port=PORT,
        ssl_certfile=str(certificate.cert_path),
        ssl_keyfile=str(certificate.key_path),
        lifespan="on",
        log_level="warning",
        proxy_headers=False,  # forwarded headers are rejected by the app anyway
        server_header=False,
    )


def browser_command(chrome: str, profile: Path, certificate: Certificate) -> list[str]:
    """Chromium flags that map the two invented names to loopback and trust only this run's certificate key."""
    return [
        chrome,
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--host-resolver-rules=MAP {HOST} {BIND}, MAP {IDP_HOST} {BIND}",
        f"--ignore-certificate-errors-spki-list={certificate.spki_sha256}",
        f"https://{HOST}/beta/app/",
    ]
