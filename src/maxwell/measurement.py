import hashlib
import json
import math
from datetime import UTC, datetime

from maxwell.records import RECORD_ADAPTER

FACTORS = ("complexity", "duplication", "coupling", "test_runtime", "test_flakiness")
ANALYZER = "python-ast-tokens-v1"
POLICY = "maintenance-v1"


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def measurement_score(record):
    if (
        record.policy_version != POLICY
        or record.analyzer_version != ANALYZER
        or not record.protocol
        or record.protocol != digest(record.policy)
    ):
        return None
    weights = record.policy.get("weights", {})
    if (
        not isinstance(weights, dict)
        or set(weights) != set(FACTORS)
        or any(
            not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
            for value in weights.values()
        )
        or not math.isclose(sum(weights.values()), 1)
    ):
        return None
    if any(
        name not in record.factors or record.factor_status.get(name) != "measured"
        for name in FACTORS
    ):
        return None
    return round(
        100
        * sum(
            weights[name]
            * min(record.factors[name].numerator / record.factors[name].denominator, 1)
            for name in FACTORS
        ),
        4,
    )


def compatible(first, second):
    return bool(
        first.protocol
        and first.protocol == second.protocol
        and first.environment_fingerprint == second.environment_fingerprint
        and first.analyzer_version == second.analyzer_version
        and first.policy_version == second.policy_version
        and first.protocol == digest(first.policy)
        and second.protocol == digest(second.policy)
        and first.repository == second.repository
        and first.policy == second.policy
    )


def projected_measurements(store, repository):
    return sorted(
        (
            RECORD_ADAPTER.validate_json(row["data"])
            for row in store.rows(
                "SELECT data FROM github_records WHERE repository=? AND type='measurement' AND status='current'",
                (repository,),
            )
        ),
        key=lambda record: (record.measured_at or 0, record.id),
    )


def historical_stage(history, policy, names):
    candidates = []
    for record in reversed(history):
        expected = (
            policy if "test_runtime" in names else {k: v for k, v in policy.items() if k != "tests"}
        )
        actual = (
            record.policy
            if "test_runtime" in names
            else {k: v for k, v in record.policy.items() if k != "tests"}
        )
        if (
            record.analyzer_version == ANALYZER
            and record.policy_version == POLICY
            and record.protocol == digest(record.policy)
            and actual == expected
            and any(
                name in record.factors and record.factor_status.get(name) == "measured"
                for name in names
            )
        ):
            candidates.append(record)
    return next(
        (
            record
            for record in candidates
            if all(
                name in record.factors and record.factor_status.get(name) == "measured"
                for name in names
            )
        ),
        candidates[0] if candidates else None,
    )


def measurement_report(record, baseline=None):
    score = measurement_score(record)
    body = f"\n### Measurement `{record.id[:12]}`\n\nRevision: `{record.sha}`; policy: `{record.policy_version}`; analyzer: `{record.analyzer_version}`.\n\n"
    body += (
        "Scope: tracked Python source and the configured test suite.\n\n"
        if record.analyzer_version == ANALYZER
        else "Scope: legacy measurement; full five-factor evidence unavailable.\n\n"
    )
    body += (
        f"Maintenance entropy score: **{score:.2f}/100** (lower is better).\n"
        if score is not None
        else "Maintenance entropy score: **unknown** — all five factors need usable evidence.\n"
    )
    label = (
        "Measured factor weight"
        if record.protocol and record.policy_version == POLICY
        else "Legacy reported coverage"
    )
    if record.evidence.get("reused"):
        label = "Available factor weight (fresh or historical)"
    body += f"{label}: {record.coverage:.0%}. Missing weights are not redistributed.\n\n"
    body += "| Factor | Raw numerator / denominator | Normalized | Status |\n| --- | --- | --- | --- |\n"
    for name in FACTORS:
        factor = record.factors.get(name)
        status = record.factor_status.get(name, "unknown")
        stage = "tests" if name.startswith("test_") else "source"
        if stage in record.evidence.get("reused", {}) and factor:
            status = "reused from history"
        raw = (
            f"{factor.numerator:g} / {factor.denominator:g} ({factor.unit})"
            if factor
            else "unknown"
        )
        normalized = f"{min(factor.numerator / factor.denominator, 1):.4f}" if factor else "unknown"
        body += f"| {name} | {raw} | {normalized} | {status.replace('|', ' ').replace(chr(10), ' ')} |\n"
    for stage, origin in record.evidence.get("reused", {}).items():
        timestamp = origin.get("measured_at")
        when = (
            datetime.fromtimestamp(timestamp, UTC).isoformat()
            if timestamp is not None
            else "unknown"
        )
        body += f"\nReused {stage} evidence: measurement `{origin['id']}` at revision `{origin['sha']}`, measured at {when}. These results were not rerun at the current revision.\n"
    tests = record.evidence.get("tests", {})
    source = record.evidence.get("source", {})
    if source:
        body += f"\nPython source coverage: {source.get('analyzed_files', 0)}/{source.get('eligible_files', 0)} eligible files; excluded files: {source.get('excluded_file_count', len(source.get('excluded_files', [])))}. Other source files not analyzed: {source.get('other_source_files', 0)}.\n"
    if tests:
        body += f"\nMeasured test runs: {tests.get('valid_runs', 0)}/{tests.get('requested_runs', 0)}; warmups: {tests.get('warmups', 0)}.\n"
        if "median_seconds" in tests:
            body += f"Median suite duration: {tests['median_seconds']:.3f}s; collected tests: {tests.get('collected_tests', 'unknown')}.\n"
        body += f"Observed flaky tests: {tests.get('observed_flaky_count', 'unknown')}; persistent failures: {tests.get('persistent_failure_count', 'unknown')}. Zero observed flakes does not establish zero flakiness.\n"
        if tests.get("slow_tests"):
            body += "\nSlow tests (median setup/call/teardown time):\n"
            for item in tests["slow_tests"][:5]:
                name = str(item["test"]).replace("`", "").replace("\n", " ")
                body += f"- `{name}`: {item['median_seconds']:.3f}s\n"
    if baseline is not None and compatible(record, baseline):
        previous = measurement_score(baseline)
        body += f"\nCompatible baseline: `{baseline.id}` at `{baseline.sha}`.\n"
        if score is not None and previous is not None:
            body += f"Score change: {score - previous:+.2f} points.\n"
        before = baseline.evidence.get("tests", {}).get("median_seconds")
        after = tests.get("median_seconds")
        if before is not None and after is not None:
            body += f"Suite duration change: {after - before:+.3f}s.\n"
        old_count = baseline.evidence.get("tests", {}).get("collected_tests")
        new_count = tests.get("collected_tests")
        if old_count is not None and new_count is not None:
            body += f"Collected test count change: {new_count - old_count:+d}.\n"
    body += "\nThis is a versioned maintenance policy, separate from agent reputation and source-assessed defects. Test evidence and runtime comparisons depend on the recorded protocol and environment.\n"
    return body
