import json
import os
import sys
from datetime import datetime


def color(text, code, stream=None):
    stream = sys.stdout if stream is None else stream
    if stream.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb":
        return f"\033[{code}m{text}\033[0m"
    return text


def check_line(check):
    labels = {"ok": "OK", "error": "FAIL", "skipped": "SKIP", "waiting": "WAIT"}
    name = check["name"]
    parts = name.split(":", 2)
    if parts[0] == "provider" and len(parts) > 1:
        name = " / ".join(parts[1:])
    elif parts[0] == "github" and len(parts) > 1:
        name = f"GitHub / {parts[1]}"
    elif parts[0] in ("repository", "stage") and len(parts) > 1:
        name = " / ".join(parts[1:])
    else:
        name = "config" if name == "configuration" else name.replace("_", " ")
    status = check["status"]
    message = check["message"]
    if status == "ok" and not message.startswith("Authenticated as "):
        message = ""
    elif status == "skipped":
        if "offline" in message:
            message = "offline"
        elif message.startswith("No default token"):
            message = "no token"
        elif "blocked by" in message:
            message = "provider errors"
    message = message.replace("\n", "\n       ")
    badge = color(
        f"[{labels[status]}]",
        {"ok": "32", "error": "31", "skipped": "33", "waiting": "36"}[status],
    )
    suffix = f": {message}" if message else ""
    print(f"{badge} {name}{suffix}", flush=True)


def measurement_line(item):
    badge = "[WAIT]" if item["heartbeat"] else "[STEP]"
    print(
        f"{color(badge, '36')} {item['repository']}: {item['phase']} ({item['elapsed']}s elapsed)",
        flush=True,
    )


def table(headers, rows, *, bordered=False, cell_style=None):
    if not rows:
        return []
    rows = [[str(value) if value is not None else "-" for value in row] for row in rows]
    widths = [
        max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)
    ]
    if not bordered:
        return [
            "  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)).rstrip()
            for row in [headers, *rows]
        ]

    def rule(left, middle, right):
        return color(left + middle.join("─" * (width + 2) for width in widths) + right, "2")

    def cells(row, heading=False):
        values = []
        for index, (value, width) in enumerate(zip(row, widths, strict=True)):
            padded = value.ljust(width)
            code = "1" if heading else cell_style(index, value) if cell_style else None
            values.append(" " + (color(padded, code) if code else padded) + " ")
        return color("│", "2") + color("│", "2").join(values) + color("│", "2")

    return [
        rule("┌", "┬", "┐"),
        cells(headers, heading=True),
        rule("├", "┼", "┤"),
        *(cells(row) for row in rows),
        rule("└", "┴", "┘"),
    ]


def model_lines(report):
    lines = [color("Models", "1;36")]
    if report.get("checked_at"):
        lines[0] += color(
            " — "
            + datetime.fromtimestamp(report["checked_at"])
            .astimezone()
            .strftime("%Y-%m-%d %H:%M %Z"),
            "2",
        )
    rows = []
    for provider in report["providers"]:
        for model in provider["models"]:
            efforts = (
                ", ".join(model["efforts"])
                if model["efforts"]
                else model["default_effort"] or ("unknown" if model["efforts"] is None else "none")
            )
            rows.append([provider["provider"], model["model"], efforts])

    def model_style(index, value):
        if value in ("unknown", "none"):
            return "2"
        if index == 0:
            return "1;36"
        if index == 1:
            return "36"
        return None

    lines.extend(
        "  " + line
        for line in table(
            ["Provider", "Model", "Efforts"],
            rows,
            bordered=True,
            cell_style=model_style,
        )
    )
    if not rows:
        lines.append("  No models stored.")
    for provider in report["providers"]:
        if provider.get("error"):
            lines.append(f"  {color('[FAIL]', '31')} {provider['provider']}: {provider['error']}")
        elif not provider["models"]:
            lines.append(f"  {provider['provider']}: no models stored.")
    if any(
        model["access"] == "unverified"
        for provider in report["providers"]
        for model in provider["models"]
    ):
        lines.append("  Unverified models could not be checked during the last apply.")
    return lines


def repository_lines(record):
    name = record.get("name", record.get("repository", ""))
    lines = [name]
    for label, key in (
        ("Branch", "ref"),
        ("GitHub account", "github_account"),
        ("Source", "source"),
    ):
        if record.get(key):
            lines.append(f"  {label}: {record[key]}")
    if "directory" in record:
        lines.append(f"  Local directory: {record['directory'] or 'none'}")
    return lines


def human_lines(value, command, repo_command=None):
    if command == "config":
        if "checks" not in value:
            return [f"Created {value['config']}. Edit it, then run `maxwell config apply`."]
        counts = {
            status: sum(check["status"] == status for check in value["checks"])
            for status in ("ok", "error", "skipped")
        }
        summary = ", ".join(
            f"{counts[status]} {label}"
            for status, label in (("ok", "passed"), ("error", "failed"), ("skipped", "skipped"))
            if counts[status]
        )
        result = color(
            "Applied" if value["valid"] else "Apply has errors", "32" if value["valid"] else "31"
        )
        lines = [f"\n{result}: {summary}."]
        if not value["valid"]:
            lines.append(f"Config: {value['config']}")
        return lines
    if command == "repo":
        if repo_command == "list":
            return [line for record in value for line in repository_lines(record)] or [
                "No repositories registered."
            ]
        verb = "Registered" if repo_command == "add" else "Updated"
        lines = repository_lines(value)
        lines[0] = f"{verb} {lines[0]}"
        return lines
    if command == "scan" and value.get("pipeline"):
        lines = []
        for summary in value.get("pipeline_repositories", []):
            repository = summary["repository"]
            counts = ", ".join(
                f"{stage}: {count}" for stage, count in summary["executions"].items()
            )
            lines.append(
                f"{color(repository, '1;36')}: {summary['closed']} closed; {summary['rejected']} rejected; {summary['remaining']} remaining; {summary['failed']} failed executions"
            )
            if counts:
                lines.append(counts)
            for reason in summary["waiting"][:3]:
                lines.append(f"[WAIT] {reason}")
        for error in value.get("errors", [])[-3:]:
            lines.append(f"[FAIL] {error['repository']} / {error['stage']}: {error['error']}")
        for status in value.get("statuses", []):
            lines.append(f"{color('Status:', '1;36')} {color(status['url'], '36;4')}")
        return lines or ["No repositories to scan."]
    if command == "scan":
        scans = value.get("scans", [])
        measurements = value.get("measurements", [])
        skipped = value.get("skipped", [])
        errors = value.get("errors", [])
        repositories = sorted(
            {
                item["repository"]
                for item in [*scans, *measurements, *skipped, *errors, *value.get("statuses", [])]
            }
        )
        if not repositories:
            return ["No repositories to scan."]
        lines = []
        for repository in repositories:
            runs = [item for item in scans if item["repository"] == repository]
            successful = [
                item
                for item in runs
                if item["state"] == "completed" and not item["publication_errors"]
            ]
            parts = (
                [
                    f"{sum(item['admitted'] for item in successful if item['stage'] == 'hunt')} new findings"
                ]
                if any(item["stage"] == "hunt" for item in runs)
                else []
            )
            for stage in ("validate", "audit"):
                verdicts = [
                    assessment["verdict"]
                    for item in successful
                    if item["stage"] == stage
                    for assessment in item.get("assessments", [])
                ]
                if verdicts:
                    counts = [
                        f"{verdicts.count(verdict)} {verdict}"
                        for verdict in ("supported", "rejected", "inconclusive")
                        if verdict in verdicts
                    ]
                    parts.append(f"{stage}: {', '.join(counts)}")
            measurement = next(
                (item for item in measurements if item["repository"] == repository), None
            )
            if measurement:
                score = measurement.get("score")
                parts.append(f"entropy {score:.2f}/100" if score is not None else "entropy unknown")
            lines.append(f"{color(repository, '1;36')}: {'; '.join(parts) or 'scan finished'}")
            failures = {}
            for item in errors:
                if item["repository"] == repository:
                    key = (item["stage"], item["error"])
                    failures[key] = failures.get(key, 0) + 1
            for item in runs:
                if item["state"] != "completed" or item["publication_errors"]:
                    key = (
                        item["stage"],
                        item["error"] or "; ".join(item["publication_errors"]) or item["state"],
                    )
                    failures[key] = failures.get(key, 0) + 1
            for (stage, message), count in failures.items():
                suffix = f" ({count} executions)" if count > 1 else ""
                lines.append(f"[FAIL] {repository} / {stage}: {message}{suffix}")
            for item in skipped:
                if item["repository"] == repository:
                    lines.append(f"[SKIP] {repository} / {item['stage']}: {item['reason']}")
            if measurement:
                unknown = [
                    f"{factor}: {status}"
                    for factor, status in measurement.get("factor_status", {}).items()
                    if status != "measured"
                ]
                if unknown:
                    lines.append("Unknown factors: " + "; ".join(unknown))
            for item in runs:
                for validation in item.get("validations", []):
                    from maxwell.measurement import validation_changes
                    from maxwell.records import RECORD_ADAPTER

                    reduction = validation_changes(
                        RECORD_ADAPTER.validate_python(
                            {
                                key: value
                                for key, value in validation.items()
                                if key not in ("issue", "publication_state")
                            }
                        )
                    )["reduction"]
                    delta = (
                        f"{reduction:+.2f} points observed reduction"
                        if reduction is not None
                        else "observed reduction unknown"
                    )
                    lines.append(f"review: {validation['outcome']} · {delta}")
                for link in item.get("pull_requests", []):
                    lines.append(f"fix PR: https://github.com/{repository}/pull/{link['number']}")
                if not item.get("pull_requests"):
                    for repair in item.get("repairs", []):
                        lines.append(f"fix: {repair['outcome']} · PR publication pending")
            status = next(
                (item for item in value.get("statuses", []) if item["repository"] == repository),
                None,
            )
            lines.append(
                f"{color('Status:', '1;36')} {color(status['url'], '36;4')}"
                if status
                else "Status: unavailable (not published to GitHub)"
            )
        return lines
    return []


def output(value, args):
    text = (
        json.dumps(value, indent=2)
        if args.json
        else "\n".join(human_lines(value, args.command, getattr(args, "repo_command", None)))
    )
    print(text, flush=True)
