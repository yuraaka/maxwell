import asyncio
import json

import pytest

from maxwell.contracts import Finding, SelectionStats, Usage
from maxwell.github import GitHub
from maxwell.records import decode_record
from maxwell.reporting import Reporter, record_report, scan_report, token_summary
from maxwell.repositories import git
from maxwell.store import Store


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
        ),
        Usage(
            input_tokens=200,
            cached_input_tokens=50,
            output_tokens=20,
            reported=True,
            complete=False,
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
        assert "Not implemented — measurement adapters and scoring policy pending" in dashboard
        bodies = [issue["body"] for issue in github["issues"].values()] + [
            comment["body"] for comments in github["comments"].values() for comment in comments
        ]
        assert all("$" not in body and "spending" not in body.lower() for body in bodies)
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
    finally:
        client.close()
        store.close()
