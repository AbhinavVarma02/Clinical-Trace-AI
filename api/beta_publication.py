"""Publication copy of one verified LIVE record view, for an owner-approved read-only demonstration.

The copy is the accepted projection from ``api.beta_live_record.live_record_view``. Nothing new is read from the
generation. The acquisition attempt identity is withheld, and the copy is labelled as a published snapshot that is
never refreshed. Whether a copy may be published at all is a separate owner decision; this module never publishes.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

FORMAT = "clinical-trace-epic-beta-publication/1"
WITHHELD = ("attempt_id",)
ORIGINS = {"epic_sandbox": False, "synthetic_live_fixture": True}  # data_origin -> synthetic_transport
LABELS = {"epic_sandbox": "Approved LIVE patient", "synthetic_live_fixture": "Synthetic LIVE-shaped fixture"}


def _consistent(view: dict[str, Any], origin: str) -> bool:
    """Every synthetic marker the projection sets must agree with the origin; one flipped flag is not enough."""
    synthetic = ORIGINS[origin]
    notes = view["record"].get("notes")
    return (
        view.get("label") == LABELS[origin]
        and any(warning.get("code") == "synthetic_live_fixture" for warning in view.get("warnings", [])) is synthetic
        and isinstance(notes, dict)
        and all(item.get("source", {}).get("synthetic") is synthetic for item in notes.get("items", []))
    )


def publication_document(view: dict[str, Any], *, exported_at: float) -> dict[str, Any]:
    """Return the publication document for one verified LIVE view, or raise ValueError."""
    origin = view.get("data_origin")
    provenance = view.get("provenance")
    active = view.get("active")
    if (
        view.get("mode") != "LIVE"
        or origin not in ORIGINS
        or view.get("source") != "epic-fhir-sandbox-r4"
        or view.get("completeness") != "unconfirmed"
        or view.get("evidence_error") is not None
        or not isinstance(view.get("record"), dict)
        or not isinstance(active, dict)
        or not isinstance(provenance, dict)
        or provenance.get("synthetic_transport") is not ORIGINS[origin]
        or provenance.get("generation_id") != active.get("generation_id")
        or not _consistent(view, origin)
    ):
        raise ValueError("publication requires a verified LIVE record view")
    published = copy.deepcopy(view)
    for key in WITHHELD:
        published["provenance"].pop(key, None)
    published["serving"] = published["freshness"] = "stale"
    published["publication"] = "published_snapshot"
    retrieved = datetime.fromtimestamp(active["retrieved_at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    published["warnings"] = [
        {
            "code": "published_snapshot",
            "severity": "warning",
            "message": f"Published read-only copy of the retrieval at {retrieved}. It is never refreshed; "
            "the sandbox record may have changed since.",
        },
        {
            "code": "clinical_ai_disabled",
            "severity": "warning",
            "message": "Clinical AI and all owner operations are disabled in this demonstration.",
        },
        *(
            warning
            for warning in published["warnings"]
            if warning["code"] not in {"live_access_disabled", "clinical_ai_disabled"}
            and not warning["code"].startswith("serving_")
        ),
    ]
    return {"format": FORMAT, "exported_at": exported_at, "withheld": list(WITHHELD), "view": published}
