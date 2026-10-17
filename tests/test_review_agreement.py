import pytest

from evaluation.review_agreement import FIELDS, agreement


def row(case, reviewer, rating):
    return {"case_id": case, "reviewer_id": reviewer, **dict.fromkeys(FIELDS, rating)}


def test_formula_with_artificial_labels_not_actual_review_evidence():
    rows = [row("a", "r1", "pass"), row("a", "r2", "pass"), row("b", "r1", "fail"), row("b", "r2", "pass")]
    result = agreement(rows, {"a", "b"})
    assert result["metrics"]["grounding"]["agreement"] == 0.5
    assert result["metrics"]["grounding"]["cohen_kappa"] == 0.0
    assert "r1" not in str(result)


def test_undefined_kappa_and_incomplete_annotations_are_not_fabricated():
    result = agreement([row("a", "r1", "pass"), row("a", "r2", "pass")], {"a"})
    assert result["metrics"]["safety"]["cohen_kappa"] is None
    with pytest.raises(ValueError):
        agreement([], {"a"})
    with pytest.raises(ValueError, match="both reviewers"):
        agreement([row("a", "r1", "pass"), row("a", "r2", "pass"), row("b", "r1", "fail")], {"a", "b"})
    with pytest.raises(ValueError, match="duplicate"):
        agreement([row("a", "r1", "pass"), row("a", "r2", "pass"), row("a", "r1", "fail")], {"a"})
