import math

import pytest

from evals.runner.metrics import Span, ndcg, precision_at, recall_at, reciprocal_rank
from evals.runner.statistics import paired_bootstrap, quantile, wilson


def test_span_metrics_count_duplicate_evidence_once_and_keep_empty_failures():
    gold = [Span("A-v2", 10, 20), Span("B-v1", 2, 8)]
    found = [Span("A-v1", 0, 30), Span("A-v2", 0, 30), Span("A-v2", 5, 25), Span("B-v1", 0, 10)]
    assert recall_at(gold, found, 3) == 0.5
    assert recall_at(gold, found, 4) == 1
    assert precision_at(gold, found, 4) == 0.5
    assert reciprocal_rank(gold, found) == 0.5
    assert recall_at(gold, [], 20) == precision_at(gold, [], 8) == reciprocal_rank(gold, []) == 0
    assert recall_at([], found, 20) is None
    assert recall_at([gold[0], gold[0]], found, 20) == 1
    assert recall_at([Span("A-v2", 10, 20)], [Span("A-v2", 11, 20)], 20) == 0


def test_ndcg_requires_ideal_reviewed_pool_and_does_not_invent_missing_gold():
    assert ndcg([2, 1, 0], [2, 1]) == 1
    assert 0 < ndcg([0, 1, 2], [2, 1]) < 1
    assert ndcg([], [2]) == 0
    assert ndcg([0], []) is None
    with pytest.raises(ValueError):
        ndcg([2, 2], [1])


def test_wilson_handles_zero_all_and_no_trials():
    assert wilson(0, 0).value is None
    assert wilson(0, 10).ci_low == 0
    assert wilson(10, 10).ci_high == 1
    result = wilson(5, 10)
    assert result.value == 0.5 and result.n == 10
    assert result.ci_low == pytest.approx(0.23659309)
    assert result.ci_high == pytest.approx(0.76340691)
    for success, n in [(11, 10), (-1, 10), (True, 10)]:
        with pytest.raises(ValueError):
            wilson(success, n)


def test_paired_hierarchical_bootstrap_is_reproducible_and_repetitions_not_new_cases():
    left = {"a": [0, 0, 0], "b": [0.5, 0.5, 0.5], "c": [1, 1, 1]}
    right = {"a": [0.25, 0.25, 0.25], "b": [0.75, 0.75, 0.75], "c": [1, 1, 1]}
    families = {"a": "first", "b": "first", "c": "second"}
    result = paired_bootstrap(left, right, families, resamples=1000)
    assert result == paired_bootstrap(left, right, families, resamples=1000)
    assert result.n == 3 and result.clusters == 2
    assert result.value == pytest.approx(1 / 6)
    assert result.ci_low <= result.value <= result.ci_high
    assert paired_bootstrap(left, left, families, resamples=100).value == 0
    assert paired_bootstrap({}, {}, {}).n == 0


@pytest.mark.parametrize("fault", ["missing_case", "missing_family", "repetition", "nan"])
def test_pairing_errors_cannot_silently_drop_failed_cases(fault):
    left = {"a": [0.0], "b": [0.0]}
    right = {"a": [1.0], "b": [1.0]}
    families = {"a": "one", "b": "two"}
    if fault == "missing_case":
        del right["b"]
    elif fault == "missing_family":
        del families["b"]
    elif fault == "repetition":
        right["b"].append(1)
    else:
        right["b"] = [math.nan]
    with pytest.raises(ValueError):
        paired_bootstrap(left, right, families, resamples=100)


def test_quantile_interpolates_and_rejects_nonfinite_values():
    assert quantile([0, 10], 0.95) == 9.5
    with pytest.raises(ValueError):
        quantile([math.inf], 0.5)
