"""Central, default-deny authorisation policy (policy-as-code).

Every protected operation calls ``authorize(context, action, patient_id)``.
Unknown personas, unknown actions, and patient-level actions outside the
persona's panel are denied. Decisions are written to the audit sink.

The public demo uses fixed synthetic personas selected with the
``X-Demo-Persona`` header. This demonstrates access control; it is not
authentication and makes no compliance claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

ALL_SYNTHETIC_PATIENTS = ("synthetic_p001", "synthetic_p002", "synthetic_p003", "synthetic_p004")


class Action(str, Enum):
    PATIENT_LIST = "patient:list"
    PATIENT_READ = "patient:read"
    RISK_READ = "risk:read"
    RETRIEVAL_SEARCH = "retrieval:search"
    AGENT_RUN = "agent:run"
    FEEDBACK_SUBMIT = "feedback:submit"
    REVIEW_SUBMIT = "review:submit"
    REVIEW_READ = "review:read"
    REVIEW_DECIDE = "review:decide"
    AUDIT_READ = "audit:read"
    OPS_READ = "ops:read"
    EVALUATION_READ = "evaluation:read"
    DATA_INGEST = "data:ingest"


PATIENT_SCOPED_ACTIONS = frozenset({
    Action.PATIENT_READ, Action.RISK_READ, Action.RETRIEVAL_SEARCH, Action.AGENT_RUN,
    Action.FEEDBACK_SUBMIT, Action.REVIEW_SUBMIT, Action.REVIEW_DECIDE,
})

ROLE_PERMISSIONS: dict[str, frozenset[Action]] = {
    "clinician": frozenset({
        Action.PATIENT_LIST, Action.PATIENT_READ, Action.RISK_READ, Action.RETRIEVAL_SEARCH,
        Action.AGENT_RUN, Action.FEEDBACK_SUBMIT, Action.REVIEW_SUBMIT, Action.REVIEW_READ,
        Action.EVALUATION_READ,
    }),
    # Reviewers inspect evidence and decide; they do not run the agent.
    "reviewer": frozenset({
        Action.PATIENT_LIST, Action.PATIENT_READ, Action.RISK_READ, Action.RETRIEVAL_SEARCH,
        Action.REVIEW_READ, Action.REVIEW_DECIDE, Action.EVALUATION_READ,
    }),
    # Administrators operate the platform but cannot read patient-level data.
    "admin": frozenset({
        Action.AUDIT_READ, Action.OPS_READ, Action.EVALUATION_READ, Action.DATA_INGEST, Action.REVIEW_READ,
    }),
}


@dataclass(frozen=True)
class Persona:
    persona_id: str
    display_name: str
    role: str
    panel: tuple[str, ...] = field(default_factory=tuple)
    description: str = ""


PERSONAS: dict[str, Persona] = {
    p.persona_id: p
    for p in (
        Persona("clinician.demo", "Demo Clinician", "clinician", ALL_SYNTHETIC_PATIENTS,
                "Synthetic clinician with access to all four synthetic patients."),
        Persona("clinician.restricted", "Restricted Clinician", "clinician", ("synthetic_p001",),
                "Synthetic clinician whose panel contains only synthetic_p001 (access-control demo)."),
        Persona("reviewer.demo", "Demo Reviewer", "reviewer", ALL_SYNTHETIC_PATIENTS,
                "Synthetic reviewer who approves or rejects agent outputs."),
        Persona("admin.demo", "Demo Administrator", "admin", (),
                "Synthetic administrator: operations and audit only, no patient data."),
    )
}


@dataclass(frozen=True)
class AccessContext:
    persona: Persona
    request_id: str | None = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


AuditSink = Callable[[AccessContext | None, str, str, Decision], None]


def resolve_persona(persona_id: str | None) -> Persona | None:
    if not persona_id:
        return None
    return PERSONAS.get(persona_id.strip())


def evaluate(context: AccessContext | None, action: Action | str, patient_id: str | None = None) -> Decision:
    if context is None:
        return Decision(False, "no access context (default deny)")
    try:
        action = Action(action)
    except ValueError:
        return Decision(False, f"unknown action '{action}' (default deny)")
    permissions = ROLE_PERMISSIONS.get(context.persona.role, frozenset())
    if action not in permissions:
        return Decision(False, f"role '{context.persona.role}' lacks permission '{action.value}'")
    if action in PATIENT_SCOPED_ACTIONS:
        if not patient_id:
            return Decision(False, "patient-scoped action without a patient id")
        if patient_id not in context.persona.panel:
            return Decision(False, f"patient '{patient_id}' is outside the persona's panel")
    return Decision(True, "allowed by role and panel")


class AuthorizationError(PermissionError):
    def __init__(self, decision: Decision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


class PolicyEngine:
    """Evaluates decisions and records each one through the audit sink."""

    def __init__(self, audit_sink: AuditSink | None = None) -> None:
        self.audit_sink = audit_sink

    def authorize(
        self, context: AccessContext | None, action: Action | str, patient_id: str | None = None
    ) -> Decision:
        decision = evaluate(context, action, patient_id)
        if self.audit_sink is not None:
            resource = f"Patient/{patient_id}" if patient_id else "-"
            action_name = action.value if isinstance(action, Action) else str(action)
            self.audit_sink(context, action_name, resource, decision)
        return decision

    def require(self, context: AccessContext | None, action: Action | str, patient_id: str | None = None) -> None:
        decision = self.authorize(context, action, patient_id)
        if not decision.allowed:
            raise AuthorizationError(decision)
