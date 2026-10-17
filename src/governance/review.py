"""Human review of agent outputs, bound to exact artifact versions.

* An *artifact* is the reviewable content of an agent run (question, verified
  claims, summary, risk block, cited evidence ids). Each version has a SHA-256
  content hash.
* A reviewer decides on a specific ``(artifact_id, version, hash)``. If the hash
  the reviewer saw differs from the current version, the decision is rejected.
* Revising an artifact creates a new version; open or approved reviews of older
  versions become ``superseded`` and a new ``pending_review`` is opened.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from src.errors import NotFoundError
from src.governance.audit import AuditLog
from src.persistence.db import Database, dumps, utc_now
from src.safety.policy import AccessContext, Action, PolicyEngine

DECISIONS = {"approved", "rejected", "needs_revision"}
OPEN_STATES = {"pending_review", "needs_revision"}


class ReviewError(ValueError):
    pass


class ReviewConflict(ReviewError):
    pass


def artifact_hash(content: dict[str, Any]) -> str:
    return hashlib.sha256(dumps(content).encode("utf-8")).hexdigest()


def artifact_content_from_run(run: dict[str, Any]) -> dict[str, Any]:
    answer = run.get("answer") or {}
    return {
        "run_id": run["run_id"],
        "patient_id": run["patient_id"],
        "question": run["question"],
        "outcome": run["outcome"],
        "summary": answer.get("summary"),
        "claims": [{"text": c["text"], "citations": c["citations"]} for c in answer.get("claims", [])],
        "uncertainty": answer.get("uncertainty", []),
        "risk": run.get("risk"),
        "inference_mode": (run.get("inference") or {}).get("mode"),
        "reviewer_note": None,
    }


class ReviewService:
    def __init__(self, db: Database, policy: PolicyEngine, audit: AuditLog) -> None:
        self.db = db
        self.policy = policy
        self.audit = audit

    # ------------------------------------------------------------ artifacts
    def _current_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM artifact WHERE artifact_id=? ORDER BY version DESC LIMIT 1", (artifact_id,))
        if row is None:
            return None
        return {**dict(row), "content": json.loads(row["content"])}

    def _write_artifact(self, artifact_id: str, version: int, run_id: str | None, patient_id: str,
                        content: dict[str, Any], persona: str) -> str:
        digest = artifact_hash(content)
        self.db.execute(
            "INSERT INTO artifact(artifact_id, version, run_id, patient_id, content, content_hash, created_by, "
            "created_at) VALUES (?,?,?,?,?,?,?,?)",
            (artifact_id, version, run_id, patient_id, dumps(content), digest, persona, utc_now()),
        )
        return digest

    def _open_review(self, artifact_id: str, version: int, digest: str, patient_id: str, persona: str) -> str:
        review_id = str(uuid.uuid4())
        now = utc_now()
        self.db.execute(
            "INSERT INTO review(review_id, artifact_id, artifact_version, artifact_hash, patient_id, status, "
            "submitted_by, created_at, updated_at) VALUES (?,?,?,?,?, 'pending_review', ?, ?, ?)",
            (review_id, artifact_id, version, digest, patient_id, persona, now, now),
        )
        self._event(review_id, persona, "submitted", None, "pending_review", digest, None)
        return review_id

    def _event(self, review_id: str, actor: str, action: str, from_status: str | None, to_status: str,
               digest: str, comment: str | None) -> None:
        self.db.execute(
            "INSERT INTO review_event(event_id, review_id, actor, action, from_status, to_status, artifact_hash, "
            "comment, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), review_id, actor, action, from_status, to_status, digest, comment, utc_now()),
        )

    # ------------------------------------------------------------- workflow
    def submit_run(self, access: AccessContext, run: dict[str, Any]) -> dict[str, Any]:
        patient_id = run["patient_id"]
        self.policy.require(access, Action.REVIEW_SUBMIT, patient_id)
        if run["outcome"] != "answered":
            raise ReviewError("only answered runs can be submitted for review")
        artifact_id = run["run_id"]
        existing = self._current_artifact(artifact_id)
        if existing is not None:
            open_review = self.db.one(
                "SELECT review_id FROM review WHERE artifact_id=? AND status IN ('pending_review','needs_revision',"
                "'approved') ORDER BY created_at DESC LIMIT 1", (artifact_id,))
            if open_review:
                return self.get(access, open_review["review_id"])
        content = artifact_content_from_run(run)
        digest = self._write_artifact(artifact_id, 1, run["run_id"], patient_id, content, access.persona.persona_id)
        review_id = self._open_review(artifact_id, 1, digest, patient_id, access.persona.persona_id)
        self.audit.event(access, "review:submitted", f"Review/{review_id}", "artifact v1 submitted")
        return self.get(access, review_id)

    def revise(self, access: AccessContext, review_id: str, remove_claims: list[int], note: str | None
               ) -> dict[str, Any]:
        review = self._review_row(review_id)
        self.policy.require(access, Action.REVIEW_SUBMIT, review["patient_id"])
        if access.persona.persona_id != review["submitted_by"]:
            raise ReviewError("only the submitter can revise an artifact")
        current = self._current_artifact(review["artifact_id"])
        assert current is not None
        content = json.loads(json.dumps(current["content"]))
        keep = [c for i, c in enumerate(content["claims"]) if i not in set(remove_claims)]
        if not keep:
            raise ReviewError("a revision must keep at least one claim")
        content["claims"] = keep
        content["reviewer_note"] = (note or "")[:500] or None
        new_version = int(current["version"]) + 1
        digest = self._write_artifact(review["artifact_id"], new_version, current["run_id"], review["patient_id"],
                                      content, access.persona.persona_id)
        superseded = self.db.all(
            "SELECT review_id, status, artifact_hash FROM review WHERE artifact_id=? AND status IN "
            "('pending_review','needs_revision','approved')", (review["artifact_id"],))
        for row in superseded:
            self.db.execute("UPDATE review SET status='superseded', updated_at=? WHERE review_id=?",
                            (utc_now(), row["review_id"]))
            self._event(row["review_id"], access.persona.persona_id, "superseded", row["status"], "superseded",
                        row["artifact_hash"], f"artifact revised to v{new_version}")
        new_review = self._open_review(review["artifact_id"], new_version, digest, review["patient_id"],
                                       access.persona.persona_id)
        self.audit.event(access, "review:revised", f"Review/{new_review}", f"artifact v{new_version} created")
        return self.get(access, new_review)

    def decide(self, access: AccessContext, review_id: str, decision: str, artifact_hash_seen: str,
               comment: str | None) -> dict[str, Any]:
        if decision not in DECISIONS:
            raise ReviewError(f"decision must be one of {sorted(DECISIONS)}")
        review = self._review_row(review_id)
        self.policy.require(access, Action.REVIEW_DECIDE, review["patient_id"])
        if review["status"] not in OPEN_STATES:
            raise ReviewConflict(f"review is {review['status']} and cannot be decided")
        if access.persona.persona_id == review["submitted_by"]:
            raise ReviewError("the submitter cannot review their own artifact")
        current = self._current_artifact(review["artifact_id"])
        assert current is not None
        if artifact_hash_seen != review["artifact_hash"] or current["content_hash"] != review["artifact_hash"]:
            raise ReviewConflict("artifact changed since it was displayed; reload before deciding")
        if decision in ("rejected", "needs_revision") and not (comment or "").strip():
            raise ReviewError("a comment is required when rejecting or requesting revision")
        self.db.execute("UPDATE review SET status=?, updated_at=? WHERE review_id=?", (decision, utc_now(), review_id))
        self._event(review_id, access.persona.persona_id, "decided", review["status"], decision,
                    review["artifact_hash"], (comment or "")[:1000] or None)
        self.audit.event(access, f"review:{decision}", f"Review/{review_id}",
                         f"decision on artifact v{review['artifact_version']}")
        return self.get(access, review_id)

    # ---------------------------------------------------------------- reads
    def _review_row(self, review_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM review WHERE review_id=?", (review_id,))
        if row is None:
            raise NotFoundError("review not found")
        return dict(row)

    def approval_valid(self, review: dict[str, Any]) -> bool:
        current = self._current_artifact(review["artifact_id"])
        return bool(review["status"] == "approved" and current and current["content_hash"] == review["artifact_hash"])

    def get(self, access: AccessContext, review_id: str) -> dict[str, Any]:
        review = self._review_row(review_id)
        self.policy.require(access, Action.REVIEW_READ)
        can_read_patient = self.policy.authorize(access, Action.PATIENT_READ, review["patient_id"]).allowed
        artifact = self.db.one("SELECT * FROM artifact WHERE artifact_id=? AND version=?",
                               (review["artifact_id"], review["artifact_version"]))
        events = self.db.all("SELECT actor, action, from_status, to_status, artifact_hash, comment, created_at "
                             "FROM review_event WHERE review_id=? ORDER BY created_at", (review_id,))
        return {
            **review,
            "approval_valid": self.approval_valid(review),
            "artifact": ({"version": artifact["version"], "content_hash": artifact["content_hash"],
                          "content": json.loads(artifact["content"])}
                         if artifact and can_read_patient else None),
            "content_visible": can_read_patient,
            "events": [dict(e) for e in events],
        }

    def list(self, access: AccessContext, status: str | None = None) -> list[dict[str, Any]]:
        self.policy.require(access, Action.REVIEW_READ)
        rows = self.db.all("SELECT * FROM review ORDER BY updated_at DESC LIMIT 200")
        items = []
        for row in rows:
            review = dict(row)
            if status and review["status"] != status:
                continue
            if access.persona.role != "admin" and review["patient_id"] not in access.persona.panel:
                continue
            review["approval_valid"] = self.approval_valid(review)
            items.append(review)
        return items
