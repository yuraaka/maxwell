import pytest
from pydantic import ValidationError

from maxwell.config import Limits
from maxwell.contracts import Finding, ScanResult, SelectionStats
from maxwell.selection import select_candidates


def candidate(finding, title, **changes):
    return Finding(**{**finding, "title": title, **changes})


def select(items, source, **changes):
    return select_candidates(
        items,
        "acme/repo",
        source,
        **{
            "publication_limit": 2,
            "slots": 6,
            "minimum_confidence": 0.8,
            "local_ids": set(),
            "remote_ids": set(),
            **changes,
        },
    )


def test_selection_ranks_before_limiting_and_counts_each_skip(source, finding):
    known = candidate(finding, "Existing known issue", priority="P0")
    high = candidate(finding, "Highest priority candidate", priority="P1", confidence=0.9)
    other = candidate(finding, "More confident normal candidate", priority="P2", confidence=0.99)
    lower = candidate(finding, "Lower ranked normal candidate", priority="P2", confidence=0.95)
    uncertain = candidate(finding, "Low confidence candidate", priority="P2", confidence=0.7)
    items = [lower, other, uncertain, known, high, high]
    selected, observed, stats = select(
        items,
        source,
        local_ids={known.identity("acme/repo")},
    )
    assert selected == [high, other] and observed == [known]
    assert stats.model_dump() == {
        "collected": 6,
        "selected": 2,
        "skipped": 4,
        "below_confidence": 1,
        "duplicate": 1,
        "already_known": 1,
        "publication_limit": 1,
        "review_queue_limit": 0,
    }
    assert select(list(reversed(items)), source, local_ids={known.identity("acme/repo")}) == (
        selected,
        observed,
        stats,
    )


def test_remote_findings_do_not_consume_new_slots_or_create_local_observations(source, finding):
    known = candidate(finding, "Remote existing finding", priority="P0")
    fresh = candidate(finding, "New candidate to publish")
    selected, observed, stats = select(
        [known, fresh],
        source,
        publication_limit=1,
        remote_ids={known.identity("acme/repo")},
    )
    assert selected == [fresh] and observed == []
    assert stats.already_known == 1 and stats.publication_limit == 0


@pytest.mark.parametrize("slots,selected_count", [(1, 1), (0, 0), (-1, 0)])
def test_selection_counts_review_queue_capacity_separately(source, finding, slots, selected_count):
    items = [candidate(finding, f"Concrete candidate {index}") for index in range(4)]
    selected, _, stats = select(items, source, publication_limit=4, slots=slots)
    assert len(selected) == selected_count
    assert stats.review_queue_limit == 4 - selected_count and stats.publication_limit == 0


def test_selection_uses_kind_and_identity_for_stable_ties(source, finding):
    items = [
        candidate(finding, "Documentation candidate", kind="documentation"),
        candidate(finding, "Concrete correctness candidate", kind="bug"),
        candidate(finding, "Other correctness candidate", kind="bug"),
    ]
    selected, _, _ = select(items, source, publication_limit=3)
    assert [item.kind for item in selected] == ["bug", "bug", "documentation"]
    assert select(list(reversed(items)), source, publication_limit=3)[0] == selected


def test_invalid_location_remains_an_error_even_outside_publication_limit(source, finding):
    valid = candidate(finding, "Highest priority candidate", priority="P0")
    invalid = candidate(finding, "Invalid outside snapshot candidate", path="../secret.txt")
    with pytest.raises(ValueError, match="outside repository"):
        select([valid, invalid], source, publication_limit=1)


@pytest.mark.parametrize("limit", [1, 100, 125])
def test_candidate_contract_uses_configured_collection_bound(finding, limit):
    items = [finding] * limit
    result_type = ScanResult.bounded(limit)
    assert len(result_type(summary="Discovery result", findings=items).findings) == limit
    assert result_type.model_json_schema()["properties"]["findings"]["maxItems"] == limit
    with pytest.raises(ValidationError):
        result_type(summary="Discovery result", findings=items + [finding])
    with pytest.raises(ValidationError, match="do not reconcile"):
        SelectionStats(collected=5, selected=1, skipped=4)


def test_candidate_limit_defaults_and_validation():
    assert Limits().max_candidates_per_scan == 100
    assert Limits(max_candidates_per_scan=125).max_candidates_per_scan == 125
    for value in (0, -1, 1.5):
        with pytest.raises(ValidationError):
            Limits(max_candidates_per_scan=value)
