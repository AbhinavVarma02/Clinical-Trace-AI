"""Request contracts for the ``/api/v2`` clinical workspace API."""

from __future__ import annotations

from typing import Literal, Optional

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
