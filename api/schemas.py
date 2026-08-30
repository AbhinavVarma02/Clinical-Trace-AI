"""Pydantic request/response schemas for the FastAPI service.

Declares the validated input (``PredictRequest``) and the typed outputs used by
the routes in ``api.routes``. Centralizing the contract here lets FastAPI
generate the OpenAPI docs and enforce field constraints before any model code
runs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HealthResponse(BaseModel):
    status: str = "healthy"
    model_loaded: bool
    llm_mode_active: bool
    langsmith_active: bool
    timestamp: datetime


class ModelInfoResponse(BaseModel):
    model_type: str
    model_version: str
    training_date: str
    metrics: dict
    risk_threshold: float


class PredictRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    patient_id: str = Field(..., description="Synthetic patient identifier")
    age: str = Field(..., description="Age bracket, e.g., '[70-80)'")
    time_in_hospital: int = Field(..., ge=0)
    num_lab_procedures: int = Field(..., ge=0)
    num_procedures: int = Field(..., ge=0)
    num_medications: int = Field(..., ge=0)
    number_outpatient: int = Field(..., ge=0)
    number_emergency: int = Field(..., ge=0)
    number_inpatient: int = Field(..., ge=0)
    number_diagnoses: int = Field(..., ge=0)
    insulin: str = "No"
    change: str = "No"
    diabetesMed: str = "No"
    race: str = "Unknown"
    gender: str = "Unknown"
    admission_type_id: int = 0
    discharge_disposition_id: int = 0
    admission_source_id: int = 0
    diag_1: str = "other"
    diag_2: str = "other"
    diag_3: str = "other"
    max_glu_serum: str = "None"
    A1Cresult: str = "None"
    prior_encounter_count: int = Field(0, ge=0)
    prior_inpatient_count: int = Field(0, ge=0)
    prior_emergency_count: int = Field(0, ge=0)
    prior_readmission_count: int = Field(0, ge=0)
    running_mean_time_in_hospital: float = Field(0.0, ge=0)
    is_first_encounter: bool = True

    @model_validator(mode="after")
    def validate_prior_history(self) -> "PredictRequest":
        """Keep first-encounter status consistent with supplied history."""
        has_history = any(
            value > 0
            for value in (
                self.prior_encounter_count,
                self.prior_inpatient_count,
                self.prior_emergency_count,
                self.prior_readmission_count,
                self.running_mean_time_in_hospital,
            )
        )
        if "is_first_encounter" not in self.model_fields_set:
            self.is_first_encounter = not has_history
        elif self.is_first_encounter and has_history:
            raise ValueError("is_first_encounter cannot be true when prior history is supplied.")
        return self


class FeatureContribution(BaseModel):
    feature: str
    contribution: float


class PredictResponse(BaseModel):
    request_id: str
    patient_id: str
    readmission_risk: str
    risk_probability: float
    risk_threshold: float
    model_version: str
    top_features: list[FeatureContribution]
    disclaimer: str = "This is decision-support only and not medical advice."


class ExplainResponse(BaseModel):
    request_id: str
    patient_id: str
    risk_label: str
    risk_probability: float
    explanation: str
    risk_drivers: list[str]
    suggested_review_areas: list[str]
    safety_disclaimer: str = "This is decision-support only and not medical advice."
    prompt_version: str
    explanation_mode: str
    trace_id: Optional[str] = None


class FeedbackRequest(BaseModel):
    request_id: str
    patient_id: str
    feedback_type: str
    comment: Optional[str] = None


class FeedbackResponse(BaseModel):
    status: str = "received"
    request_id: str
