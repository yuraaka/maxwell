import asyncio
import json

import pytest

from maxwell.contracts import Finding, SelectionStats, Spending, Usage
from maxwell.github import GitHub
from maxwell.records import decode_record
from maxwell.reporting import Reporter, record_report, scan_report, token_summary
from maxwell.repositories import git
from maxwell.store import Store


def test_unchanged_sync_revalidates_without_charging_reads_or_recreating_labels(
    home, source, github
):
    config = home.load()
    store = Store(home.db)
    store.register("acme/repo", str(source), "HEAD")
    try:
        for _ in range(2):
            client = GitHub(config.github)
            try:
                asyncio.run(Reporter(store, client, config, home).sync(process_commands=False))
            finally:
                client.close()
        before_requests = len(github["requests"])
        before_unchanged = github["unchanged_reads"]
        client = GitHub(config.github)
        try:
            asyncio.run(Reporter(store, client, config, home).sync(process_commands=False))
        finally:
            client.close()
        requests = github["requests"][before_requests:]
        assert requests and all(method == "GET" for method, _, _ in requests)
        assert github["unchanged_reads"] - before_unchanged == len(requests)
    finally:
        store.close()


def test_projection_skips_comments_only_when_github_reports_zero(home, github, monkeypatch):
    config = home.load()
    store = Store(home.db)
    client = GitHub(config.github)
    reporter = Reporter(store, client, config, home)
    issue = client.create_issue("acme/repo", "Status", "<!-- maxwell:dashboard:v1 -->")
    comments = []

    def load_comments(repo, number):
        comments.append(number)
        return []

    monkeypatch.setattr(client, "comments", load_comments)
    try:
        asyncio.run(reporter.project_issue_records("acme/repo", {**issue, "comments": 0}))
        assert comments == []
        asyncio.run(reporter.project_issue_records("acme/repo", {**issue, "comments": 1}))
        asyncio.run(reporter.project_issue_records("acme/repo", issue))
        assert comments == [issue["number"], issue["number"]]
    finally:
        client.close()
        store.close()


@pytest.mark.parametrize(
    "reported,expected",
    [
        (True, "1,100 tokens: input 1,000 (cached 500; cache writes 100), output 100"),
        (False, "Token usage unavailable"),
    ],
)
def test_token_summary_counts_cache_once_and_preserves_unknown_usage(reported, expected):
    usage = Usage(
        input_tokens=1000,
        cached_input_tokens=500,
        cache_write_tokens=100,
        output_tokens=100,
        reported=reported,
    )
    assert token_summary(usage.model_dump()) == expected


@pytest.mark.parametrize("limit", [100, 125])
def test_collection_ceiling_is_reported_without_claiming_exhaustive_coverage(limit):
    stats = SelectionStats(
        collected=limit,
        selected=3,
        skipped=limit - 3,
        publication_limit=limit - 3,
    )
    body = scan_report(
        {
            "id": "a" * 32,
            "state": "completed",
            "summary": "Discovery result",
            "sha": "f" * 40,
            "usage": None,
            "admitted": 3,
            "selection": stats.model_dump_json(),
            "execution": json.dumps({"limits": {"max_candidates_per_scan": limit}})
            if limit != 100
            else None,
        }
    )
    assert f"Skipped: **{limit - 3}**" in body
    assert f"Collection is bounded at {limit} candidates" in body
    assert "Collection reached the candidate ceiling" in body


def test_reports_sum_execution_tokens_without_counting_findings_again(
    home, source, finding, github
):
    store = Store(home.db)
    config = home.load()
    store.register("acme/repo", str(source), "HEAD")
    execution = config.execution(config.route(model="fake-model"), store.repository("acme/repo"))
    sha = git("rev-parse", "HEAD", cwd=source)
    usages = [
        Usage(
            input_tokens=1000,
            cached_input_tokens=500,
            output_tokens=100,
            reported=True,
            complete=True,
            spending=Spending(micros=123_000, source="configured model tariff", complete=True),
        ),
        Usage(
            input_tokens=200,
            cached_input_tokens=50,
            output_tokens=20,
            reported=True,
            complete=False,
            spending=Spending(micros=12_000, source="configured model tariff"),
        ),
        Usage(),
    ]
    scans = []
    client = GitHub(config.github)
    try:
        for index, usage in enumerate(usages):
            identifier = store.enqueue("acme/repo", execution=execution)
            job = store.claim(20_000_000, 1_000_000, 6)
            with store.db:
                store.db.execute("UPDATE scans SET sha=?,executed=1 WHERE id=?", (sha, identifier))
            store.finish(
                job,
                "completed" if index == 0 else "failed",
                "Discovery result.",
                usage,
                100,
                [Finding(**finding)] if index == 0 else [],
            )
            scans.append(store.one("SELECT * FROM scans WHERE id=?", (identifier,)))
        asyncio.run(Reporter(store, client, config, home).sync("acme/repo", process_commands=False))
        dashboard = next(
            issue["body"]
            for issue in github["issues"].values()
            if issue["title"] == "Maxwell status"
        )
        assert "| Recorded input tokens | 1,200 |" in dashboard
        assert "| Recorded total tokens (input + output) | 1,320 |" in dashboard
        assert "| Cached input tokens (included in input) | 550 |" in dashboard
        assert "| Executions with unavailable token usage | 1 |" in dashboard
        assert "| Executions with incomplete token usage | 2 |" in dashboard
        assert "#### Investigation tokens" in dashboard
        assert "| hunt agent | 3 | 1,200 | 120 | 1,320 | 1 | 2 |" in dashboard
        assert "run `maxwell measure` to establish a baseline" in dashboard
        bodies = [issue["body"] for issue in github["issues"].values()] + [
            comment["body"] for comments in github["comments"].values() for comment in comments
        ]
        assert "| Recorded API-equivalent cost (USD estimate) | $0.1350 |" in dashboard
        assert "| Executions with unknown cost | 1 |" in dashboard
        assert "| Executions with partial cost | 1 |" in dashboard
        assert any("Cost: unknown" in body for body in bodies)
        record = decode_record(scan_report(scans[0]))
        assert record.accounting == "tokens" and record.cost_micros is None
        assert "1,100 tokens" in record_report(record)
        assert "Token usage unavailable" in scan_report(scans[-1])
        finding_body = next(
            issue["body"]
            for issue in github["issues"].values()
            if issue["title"] == finding["title"]
        )
        assert decode_record(finding_body).allocated_cost_micros is None
        assert decode_record(finding_body).spending == usages[0].spending
        assert "$0.1230 USD" in finding_body
        assert "shared hunt execution cost" in finding_body
    finally:
        client.close()
        store.close()


def test_dashboard_without_measurement_links_unknown_components():
    from maxwell.contracts import FACTORS
    from maxwell.reporting import component_dashboard

    body = component_dashboard("acme/repo", None)
    assert body.count("[Unknown](") == len(FACTORS)
    assert "0.00 points" not in body
    assert "Measurement evidence" not in body


def test_dashboard_removes_legacy_generated_comments(home, source, github):
    store = Store(home.db)
    store.register("acme/repo", str(source), "main")
    client = GitHub(home.load().github)
    try:
        reporter = Reporter(store, client, home.load(), home)
        issue = asyncio.run(reporter.dashboard("acme/repo"))
        client.create_comment(
            "acme/repo", issue, "<!-- maxwell:scan:" + "a" * 32 + " -->\nOld report"
        )
        client.create_comment(
            "acme/repo", issue, "<!-- maxwell:measurement:" + "b" * 32 + " -->\nOld measurement"
        )
        asyncio.run(reporter.dashboard("acme/repo"))
        assert github["comments"][issue] == []
        asyncio.run(reporter.dashboard("acme/repo"))
        assert github["comments"][issue] == []
    finally:
        client.close()
        store.close()


def test_unknown_component_explains_missing_evidence():
    from maxwell.records import MeasurementRecord
    from maxwell.reporting import component_dashboard

    record = MeasurementRecord(
        id="a" * 32,
        repository="acme/repo",
        sha="f" * 40,
        analyzer_version="legacy",
        policy_version="legacy",
        coverage=0,
        factors={},
        factor_status={"test_runtime": "Pytest unavailable"},
    )
    body = component_dashboard("acme/repo", record)
    assert ") — Pytest unavailable" in body


def test_status_cleanup_preserves_human_comment_quoting_report(home, source, github):
    store = Store(home.db)
    store.register("acme/repo", str(source), "main")
    client = GitHub(home.load().github)
    try:
        reporter = Reporter(store, client, home.load(), home)
        issue = asyncio.run(reporter.dashboard("acme/repo"))
        client.create_comment(
            "acme/repo",
            issue,
            "Question about this report:\n<!-- maxwell:scan:" + "a" * 32 + " -->",
        )
        asyncio.run(reporter.dashboard("acme/repo"))
        assert len(github["comments"][issue]) == 1
    finally:
        client.close()
        store.close()
