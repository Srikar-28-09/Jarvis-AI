import pytest

from scripts.benchmark_readiness import percentile, safe_endpoint


def test_percentile_uses_nearest_rank_for_small_samples():
    assert percentile([30.0, 10.0, 20.0], 0.5) == 20.0
    assert percentile([30.0, 10.0, 20.0], 0.95) == 30.0


def test_percentile_requires_a_sample():
    with pytest.raises(ValueError):
        percentile([], 0.5)


def test_endpoint_summary_redacts_credentials_and_query():
    assert safe_endpoint("https://user:secret@example.test/ready?token=hidden#fragment") == (
        "https://example.test/ready"
    )
