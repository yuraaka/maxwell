import hashlib
import json
import random

import pytest
import yaml

from maxwell.agent_selection import AgentSelector
from maxwell.contracts import Finding
from maxwell.records import AssessmentRecord, FindingRecord, PublicAgent
from maxwell.reputation import selection_evidence
from maxwell.store import Store


def cache_models(home, store, catalogs=None):
    config = home.load()
    if catalogs is None:
        catalogs = [
            {
                "provider": name,
                "models": [
                    {
                        "model": model,
                        "access": "verified",
                        "selectable": True,
                        "efforts": None,
                        "default_effort": None,
                    }
                    for model in (settings.models or {settings.model: None})
                ],
            }
            for name, settings in config.all_providers().items()
        ]
    store.apply_models(
        {"providers": catalogs, "valid": True, "checks": []},
        hashlib.sha256(home.config.read_bytes()).hexdigest(),
    )
    return config


@pytest.fixture
def selection(home, source):
    data = yaml.safe_load(home.config.read_text())
    settings = data.pop("provider")
    data["providers"] = {
        "private-a": {**settings, "models": ["fake-model", "other-model"]},
        "private-alias": {**settings, "models": ["fake-model"]},
    }
    home.config.write_text(yaml.safe_dump(data))
    store = Store(home.db)
    store.register("acme/repo", str(source), "main")
    config = cache_models(home, store)
    yield home, store, config
    store.close()


def observe(store, finding, *, number, review="supported", audit=None):
    item = Finding(**{**finding, "title": f"{finding['title']} case {number}"})
    record = FindingRecord(
        id=item.identity("acme/repo"),
        repository="acme/repo",
        sha="f" * 40,
        execution_id=f"{number:032x}",
        finding=item,
        agent=PublicAgent(adapter="codex", model="fake-model", role="hunter"),
    )
    store.project_record(record, number, 0, "maintainer", f"finding-{number}")
    reviewed = AssessmentRecord(
        id=f"{number + 100:032x}",
        repository=record.repository,
        finding_id=record.id,
        sha=record.sha,
        role="reviewer",
        verdict=review,
        execution_id=f"{number + 200:032x}",
        agent=PublicAgent(adapter="codex", model="other-model", role="reviewer"),
        evidence="Source evidence establishes the behavior at app.py:2.",
    )
    store.project_record(reviewed, number, number + 1000, "maintainer", f"review-{number}")
    if audit:
        audited = AssessmentRecord(
            id=f"{number + 300:032x}",
            repository=record.repository,
            finding_id=record.id,
            sha=record.sha,
            role="auditor",
            verdict=audit,
            references=[reviewed.id],
            execution_id=f"{number + 400:032x}",
            agent=PublicAgent(adapter="codex", model="third-model", role="auditor"),
            evidence="Independent source evidence at app.py:2 checks the review.",
        )
        store.project_record(audited, number, number + 2000, "maintainer", f"audit-{number}")
    return record, reviewed


def test_selection_evidence_prefers_audits_without_double_counting(selection, finding):
    _, store, _ = selection
    observe(store, finding, number=1, audit="rejected")
    observe(store, finding, number=2)
    observe(store, finding, number=3, audit="inconclusive")
    assert selection_evidence(store, "acme/repo", "hunter") == {
        ("codex", "fake-model", None): [1, 1]
    }
    assert selection_evidence(store, "acme/repo", "reviewer") == {
        ("codex", "other-model", None): [0, 1]
    }
    assert selection_evidence(store, "acme/repo", "auditor") == {}
    assert selection_evidence(store, "another/repo", "hunter") == {}


def test_selection_excludes_invalid_records_and_stale_audit_references(selection, finding):
    _, store, _ = selection
    record, review = observe(store, finding, number=1, audit="supported")
    replacement = review.model_copy(
        update={"id": "a" * 32, "verdict": "rejected", "supersedes": review.id}
    )
    store.project_record(replacement, 1, 5000, "maintainer", "replacement")
    assert selection_evidence(store, "acme/repo", "hunter")[("codex", "fake-model", None)] == [0, 1]
    assert selection_evidence(store, "acme/repo", "reviewer") == {}
    with store.db:
        store.db.execute("UPDATE github_records SET status='changed' WHERE id=?", (record.id,))
    assert selection_evidence(store, "acme/repo", "hunter") == {}


def test_aliases_do_not_multiply_public_sampling_weight(selection):
    home, store, config = selection
    selector = AgentSelector(home, config, rng=random.Random(1))
    assert len(selector.routes) == 2
    assert len(selector.routes[("codex", "fake-model", None)]) == 2
    _, decision = selector.select(store, "acme/repo", "hunt")
    assert decision["candidates"] == 2
    assert decision["successes"] == decision["failures"] == 0
    assert decision["method"] == "thompson-source-v1"


def test_sampling_responds_to_role_evidence_but_keeps_exploration(selection, finding):
    home, store, config = selection
    for number in range(1, 21):
        observe(store, finding, number=number, audit="supported")
    selector = AgentSelector(home, config, rng=random.Random(7))
    choices = [selector.select(store, "acme/repo", "hunt")[0]["model"] for _ in range(300)]
    assert 270 < choices.count("fake-model") < 300
    _, decision = selector.select(store, "acme/repo", "audit")
    assert decision["method"] == "uniform-unrated-v1" and decision["draw"] is None
    assert decision["successes"] == decision["failures"] == 0


def test_independence_avoids_same_model_across_efforts_and_reports_fallback(selection):
    home, store, config = selection
    selector = AgentSelector(home, config, rng=random.Random(2))
    route, decision = selector.select(store, "acme/repo", "review", avoid={("codex", "fake-model")})
    assert route["model"] == "other-model" and decision["independence"] == "different_model"
    _, fallback = selector.select(
        store, "acme/repo", "audit", avoid={("codex", "fake-model"), ("codex", "other-model")}
    )
    assert fallback["independence"] == "constrained"


def test_explicit_selection_preserves_private_route_and_configured_effort(selection):
    home, store, config = selection
    selector = AgentSelector(home, config, provider="private-a", model="fake-model", effort="high")
    route, decision = selector.select(store, "acme/repo", "review", avoid={("codex", "fake-model")})
    assert route == {"provider": "private-a", "model": "fake-model", "effort": "high"}
    assert decision["method"] == "explicit" and decision["independence"] == "overridden"
    assert "private-a" not in json.dumps(decision)


def test_automatic_selection_requires_current_verified_catalog(selection):
    home, store, config = selection
    home.config.write_text(home.config.read_text() + "\n# changed\n")
    with pytest.raises(ValueError, match="stale.*config apply"):
        AgentSelector(home, config)
    catalogs = [
        {
            "provider": "private-a",
            "models": [
                {"model": "fake-model", "access": "unverified"},
                {"model": "other-model", "access": "verified", "selectable": False},
            ],
        }
    ]
    config = cache_models(home, store, catalogs)
    with pytest.raises(ValueError, match="No verified selectable"):
        AgentSelector(home, config)


def test_supported_efforts_do_not_expand_sampling_pool(selection):
    home, store, config = selection
    config.providers["private-a"].efforts = ["low", "high"]
    config.providers["private-a"].effort = "high"
    selector = AgentSelector(home, config, provider="private-a")
    assert {identity[2] for identity in selector.routes} == {"high"}
    assert len(selector.routes) == 2


def test_explicit_distinct_model_still_reports_independence(selection):
    home, store, config = selection
    selector = AgentSelector(home, config, provider="private-a", model="other-model")
    _, decision = selector.select(store, "acme/repo", "review", avoid={("codex", "fake-model")})
    assert decision["method"] == "explicit" and decision["independence"] == "different_model"


def test_future_validated_ratings_can_drive_auditor_selection(selection):
    home, store, config = selection

    def validated_evidence(current_store, repository, role):
        assert current_store is store and repository == "acme/repo" and role == "auditor"
        return {("codex", "other-model", None): [100, 0]}

    selector = AgentSelector(
        home, config, rng=random.Random(1), evidence_provider=validated_evidence
    )
    route, decision = selector.select(store, "acme/repo", "audit")
    assert route["model"] == "other-model"
    assert decision["method"] == "thompson-source-v1" and decision["successes"] == 100
