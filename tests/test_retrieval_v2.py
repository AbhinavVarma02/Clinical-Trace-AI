"""Hybrid retrieval: patient isolation, filters, caching, versioning, ranking metrics."""

from __future__ import annotations

import copy

import pytest

from src.clinical.records import ClinicalRecords
from src.clinical.store import FHIRStore
from src.clinical.synthetic import FIXTURE_PATH
from src.persistence.db import Database
from src.retrieval.chunking import split_sections
from src.retrieval.index import RetrievalIndex
from src.retrieval.metrics import mrr_at_k, ndcg_at_k, precision_at_k, recall_at_k
from src.safety.guards import flag_content

PATIENTS = ("synthetic_p001", "synthetic_p002", "synthetic_p003", "synthetic_p004")


@pytest.fixture()
def index() -> RetrievalIndex:
    store = FHIRStore(Database(":memory:"))
    records = ClinicalRecords(store)
    idx = RetrievalIndex(records, flagger=flag_content)
    store.on_change(idx.refresh)
    store.ingest_bundle_file(FIXTURE_PATH)
    return idx


@pytest.mark.parametrize("patient_id", PATIENTS)
@pytest.mark.parametrize("query", ["warfarin INR", "smoker COPD FEV1", "metformin eGFR", "glucose 388",
                                   "Jordan Riley Casey Morgan", "synthetic_p002 records"])
def test_results_never_cross_patients(index: RetrievalIndex, patient_id: str, query: str) -> None:
    for mode in ("bm25", "dense", "hybrid"):
        response = index.search(patient_id, query, k=20, mode=mode)
        assert all(hit["patient_id"] == patient_id for hit in response["results"])


def test_hybrid_finds_the_metformin_stop_reason(index: RetrievalIndex) -> None:
    hits = index.search("synthetic_p001", "why was metformin stopped", k=3)["results"]
    assert any("metformin was stopped" in hit["text"] for hit in hits)


def test_temporal_filter(index: RetrievalIndex) -> None:
    hits = index.search("synthetic_p004", "INR", k=20, date_from="2025-04-01", date_to="2025-06-30")["results"]
    assert hits and all("2025-04-01" <= hit["date"][:10] <= "2025-06-30" for hit in hits)


def test_cache_hit_and_version_change_on_ingest(index: RetrievalIndex) -> None:
    first = index.search("synthetic_p001", "HbA1c")
    second = index.search("synthetic_p001", "HbA1c")
    assert not first["diagnostics"]["cache_hit"] and second["diagnostics"]["cache_hit"]
    version = index.version
    obs = copy.deepcopy(index.records.store.get("Observation", "p001-o16"))
    obs["meta"]["versionId"] = "2"
    obs["valueQuantity"]["value"] = 9.5
    index.records.store.ingest([obs], source="test-update")
    assert index.version != version
    assert not index.search("synthetic_p001", "HbA1c")["diagnostics"]["cache_hit"]
    assert index.chunk("synthetic_p001", "p001-o16@v2") is not None


def test_injection_text_is_flagged(index: RetrievalIndex) -> None:
    flagged = [c for c in index.chunks_for("synthetic_p002") if c.flags]
    assert flagged and all(c.source_ref == "DocumentReference/p002-d4" for c in flagged)


def test_unknown_patient_returns_empty(index: RetrievalIndex) -> None:
    assert index.search("synthetic_p999", "anything")["results"] == []


def test_section_splitting_offsets() -> None:
    text = "Reason for admission: A.\nHospital course: B.\nPlan: C."
    sections = split_sections(text)
    assert [s[0] for s in sections] == ["Reason for admission", "Hospital course", "Plan"]
    assert text[sections[1][2]:sections[1][3]].startswith("Hospital course")


def test_ranking_metrics() -> None:
    ranked = ["a", "b", "c", "d"]
    relevant = {"b", "d"}
    assert recall_at_k(ranked, relevant, 2) == 0.5
    assert precision_at_k(ranked, relevant, 2) == 0.5
    assert mrr_at_k(ranked, relevant, 4) == 0.5
    assert 0 < ndcg_at_k(ranked, relevant, 4) < 1
    assert ndcg_at_k(["b", "d"], relevant, 2) == pytest.approx(1.0)
