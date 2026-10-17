import pandas as pd
import pytest

from monitoring.drift_report import build_report, categorical_drift, numeric_drift


def test_numeric_reference_bins_detect_shift_and_missingness():
    reference = pd.Series(range(100))
    identical = numeric_drift(reference, reference)
    shifted = numeric_drift(reference, pd.Series(range(100, 200)))
    assert identical["psi"] == 0 and not identical["alert"]
    assert shifted["alert"] and shifted["reference_cutpoints"] == identical["reference_cutpoints"]
    missing = numeric_drift(reference, pd.Series([None] * 100))
    assert missing["alert"] and missing["current_missing_rate"] == 1


@pytest.mark.parametrize("value", [-1, 1])
def test_constant_reference_detects_both_directions(value):
    assert numeric_drift(pd.Series([0] * 20), pd.Series([value] * 20))["alert"]


def test_categorical_unseen_values_are_not_fitted_on_current():
    report = categorical_drift(pd.Series(["a", "b", "a", None]), pd.Series(["new", "new", "?", "a"]))
    assert report["reference_vocabulary"] == ["a", "b"]
    assert report["unseen_rate"] == 0.5 and report["current_missing_rate"] == 0.25
    assert sum(report["current_counts"]) == 4


def test_drift_rejects_identifiers_and_invalid_prediction_windows():
    frame = pd.DataFrame({"age": ["[50-60)"] * 3})
    with pytest.raises(ValueError, match="identifiers"):
        build_report(frame, frame, ["patient_nbr"])
    with pytest.raises(ValueError, match="aligned"):
        build_report(frame, frame, ["age"], [0.5], [0.5])
    assert build_report(frame, frame, ["age"], [0.5] * 3, [0.5] * 3)["predictions"]["psi"] == 0
