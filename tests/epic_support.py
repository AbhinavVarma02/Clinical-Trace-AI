"""Shared offline helpers for Epic sandbox adapter tests (MOCK only; no network)."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from typing import Any

import httpx

from src.interop.epic import settings
from src.interop.epic.auth import AccessToken
from src.interop.epic.client import BinaryContent
from src.interop.epic.telemetry import EpicTelemetry

FIXTURE = Path(__file__).parent / "fixtures" / "epic_sandbox" / "epic_shaped_patients.json"
RETRIEVED_AT = "2026-11-05T18:00:00Z"


def fixture_patient(name: str = "alpha") -> tuple[str, list[dict[str, Any]], dict[str, BinaryContent]]:
    patient = json.loads(FIXTURE.read_text(encoding="utf-8"))["patients"][name]
    binaries = {
        ref: BinaryContent(reference=ref, content_type=item["contentType"], data=item["text"].encode())
        for ref, item in patient["binaries"].items()
    }
    return patient["patient_id"], copy.deepcopy(patient["resources"]), binaries


class FakeTokens:
    """Stands in for EpicTokenProvider: issues fake tokens, never contacts anything."""

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


def _categories(resource: dict[str, Any]) -> set[str]:
    return {
        str(coding.get("code")) for concept in resource.get("category") or [] for coding in concept.get("coding") or []
    }


def fake_epic_transport(
    resources: list[dict[str, Any]],
    binaries: dict[str, BinaryContent],
    *,
    failures: dict[tuple[str, str | None], int] | None = None,
    requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """A mock sandbox that answers Patient.Read, category searches and Binary.Read from fixtures."""
    patient = next(r for r in resources if r["resourceType"] == "Patient")

    def respond(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        assert request.url.host == settings.EPIC_HOST and request.method == "GET"
        relative = request.url.path.split("/api/FHIR/R4/", 1)[1]
        headers = {"content-type": "application/fhir+json"}
        if relative.startswith("Patient/"):
            return httpx.Response(200, json=patient, headers=headers)
        if relative.startswith("Binary/"):
            binary = binaries[relative]
            body = {
                "resourceType": "Binary",
                "contentType": binary.content_type,
                "data": base64.b64encode(binary.data).decode(),
            }
            return httpx.Response(200, json=body, headers=headers)
        category = request.url.params.get("category")
        if failures and (relative, category) in failures:
            return httpx.Response(failures[(relative, category)])
        matches = [
            r for r in resources if r["resourceType"] == relative and (category is None or category in _categories(r))
        ]
        bundle = {
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [{"resource": r, "search": {"mode": "match"}} for r in matches],
        }
        return httpx.Response(200, json=bundle, headers=headers)

    return httpx.MockTransport(respond)
