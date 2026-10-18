"""Request contracts for the ``/api/v2`` clinical workspace API."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class AgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patient_id: str = Field(..., pattern=r"^synthetic_[a-z0-9]{1,32}$")
    question: str = Field(..., min_length=1, max_length=1000)
    session_id: Optional[str] = Field(None, max_length=64)


class RetrievalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(..., min_length=2, max_length=300)
    k: int = Field(6, ge=1, le=20)
    mode: Literal["bm25", "dense", "hybrid"] = "hybrid"
    date_from: Optional[str] = Field(None, max_length=32)
    date_to: Optional[str] = Field(None, max_length=32)


class ReviewSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(..., max_length=64)


class ReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approved", "rejected", "needs_revision"]
    artifact_hash: str = Field(..., min_length=64, max_length=64)
    comment: Optional[str] = Field(None, max_length=1000)


class ReviewRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remove_claims: list[int] = Field(default_factory=list, max_length=50)
    note: Optional[str] = Field(None, max_length=500)


class RunFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["incorrect", "unsupported", "missing_evidence", "unsafe", "helpful", "other"]
    rating: Optional[int] = Field(None, ge=1, le=5)
    comment: Optional[str] = Field(None, max_length=1000)


class ResponseContract(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)


class ClaimResponse(ResponseContract):
    text: str
    citations: list[str]


class AnswerResponse(ResponseContract):
    summary: str | None
    claims: list[ClaimResponse]
    rejected_claims: list[ClaimResponse]
    uncertainty: list[str]
    abstain_reason: str | None


class EvidenceResponse(ResponseContract):
    evidence_id: str
    patient_id: str
    source_ref: str
    source_version: str
    text: str
    kind: str
    title: str
    date: str
    flags: list[str]
    scores: dict[str, float | int | None] | None
    cited: bool


class InferenceResponse(ResponseContract):
    mode: Literal["deterministic", "llm"]
    planner: str | None
    composer: str | None
    fallback_used: bool
    llm_attempt: dict[str, Any] | None
    provider: dict[str, Any] | None
    degraded_reason: Literal["quota_exhausted", "relay_unavailable", "provider_unavailable"] | None = None
    label: str


class ReviewGateResponse(ResponseContract):
    required: bool
    reasons: list[str]


class ToolCallResponse(ResponseContract):
    tool: str
    ok: bool
    evidence_count: int = Field(ge=0)
    error_type: str | None
    error: str | None
    latency_ms: float = Field(ge=0)
    args: dict[str, Any]


class AgentRunResponse(ResponseContract):
    run_id: str
    session_id: str
    patient_id: str
    question: str
    outcome: Literal["answered", "abstained", "refused", "cancelled", "error"]
    answer: AnswerResponse
    refusal: dict[str, str] | None
    evidence: list[EvidenceResponse]
    risk: dict[str, Any] | None
    plan: dict[str, Any] | None
    tool_calls: list[ToolCallResponse]
    verification: dict[str, Any] | None
    review: ReviewGateResponse
    inference: InferenceResponse
    trace: list[dict[str, Any]]
    error: str | None
    latency_ms: float = Field(ge=0)
    created_at: str
    disclaimer: str


class RunStatusResponse(ResponseContract):
    run_id: str
    session_id: str
    status: Literal["running", "interrupted", "completed", "failed", "cancelled"]
    outcome: str | None
    cancel_requested: bool
    resume_count: int = Field(ge=0, le=3)


class RetrievalHitResponse(ResponseContract):
    chunk_id: str
    patient_id: str
    source_ref: str
    source_version: str
    content_hash: str
    text: str
    scores: dict[str, float | int | None]


class RetrievalResponse(ResponseContract):
    patient_id: str
    query: str
    mode: Literal["bm25", "dense", "hybrid"]
    results: list[RetrievalHitResponse]
    diagnostics: dict[str, Any]
