import json
import os
import sys


def color(text, code, stream=None):
    stream = sys.stdout if stream is None else stream
    if stream.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb":
        return f"\033[{code}m{text}\033[0m"
    return text


def check_line(check):
    labels = {"ok": "OK", "error": "FAIL", "skipped": "SKIP"}
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
    badge = color(f"[{labels[status]}]", {"ok": "32", "error": "31", "skipped": "33"}[status])
    suffix = f": {message}" if message else ""
    print(f"{badge} {name}{suffix}", flush=True)


def table(headers, rows):
    if not rows:
        return []
    rows = [[str(value) if value is not None else "-" for value in row] for row in rows]
    widths = [
        max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)
    ]
    return [
        "  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)).rstrip()
        for row in [headers, *rows]
    ]


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
    if "paused" in record:
        lines.append(f"  Scans: {'paused' if record['paused'] else 'enabled'}")
    for stage, route in record.get("stages", {}).items():
        lines.append(f"  {stage.capitalize()}: {route['provider']} / {route['model']}")
    return lines


def status_lines(value):
    pid = f" (PID {value['pid']})" if value.get("pid") else ""
    lines = [f"Daemon {value['daemon']}{pid}"]
    if "budget_committed_usd" in value:
        lines.append(f"Committed scan budget: ${value['budget_committed_usd']:.2f}")
    if "repositories" in value:
        lines.append("\nRepositories:")
        for repository in value["repositories"]:
            lines.extend(repository_lines(repository))
        if not value["repositories"]:
            lines.append("  No repositories registered.")
    if value.get("scans"):
        lines.append("\nRecent scans:")
        rows = []
        for scan in value["scans"]:
            if scan.get("cost") is not None:
                cost = f"${scan['cost'] / 1_000_000:.4f}"
            elif scan.get("accounting") == "unpriced":
                cost = "unpriced"
            else:
                cost = "unknown"
            rows.append(
                [
                    scan["id"],
                    scan["repository"],
                    scan["state"],
                    f"P{scan['priority']}",
                    f"{scan.get('provider') or 'legacy'} / {scan.get('model') or 'legacy'}",
                    cost,
                ]
            )
        lines.extend(
            table(["ID", "Repository", "State", "Priority", "Provider / model", "Cost"], rows)
        )
        for scan in value["scans"]:
            if scan.get("error"):
                lines.append(f"  Scan {scan['id']}: {scan['error']}")
    for key, label in (
        ("publication_problems", "Finding publication"),
        ("dashboard_problems", "Status publication"),
    ):
        for problem in value.get(key, []):
            lines.append(
                f"  {label} problem ({problem.get('repository', problem.get('id'))}): {problem.get('error') or problem['state']}"
            )
    if value.get("ratings"):
        lines.append("\nModel ratings (provisional):")
        lines.extend(
            table(
                ["Provider / model", "Rating", "Confirmed", "Rejected"],
                [
                    [
                        rating["profile"],
                        f"{rating['rating']}%",
                        rating["confirmed"],
                        rating["rejected"],
                    ]
                    for rating in value["ratings"]
                ],
            )
        )
    elif value.get("hunter_rating"):
        lines.append(f"Model ratings: {value['hunter_rating']}")
    if value.get("entropy"):
        lines.append(f"Entropy: {value['entropy']}")
    return lines


def human_lines(value, command, repo_command=None):
    if command == "config":
        counts = {
            status: sum(check["status"] == status for check in value["checks"])
            for status in ("ok", "error", "skipped")
        }
        summary = ", ".join(
            f"{counts[status]} {label}"
            for status, label in (("ok", "passed"), ("error", "failed"), ("skipped", "skipped"))
            if counts[status]
        )
        result = color("Valid" if value["valid"] else "Invalid", "32" if value["valid"] else "31")
        lines = [f"\n{result}: {summary}."]
        if not value["valid"]:
            lines.append(f"Config: {value['config']}")
        return lines
    if command == "init":
        return [f"Created {value['config']}. Configure GitHub credentials and provider homes."]
    if command == "repo":
        if repo_command == "list":
            return [line for record in value for line in repository_lines(record)] or [
                "No repositories registered."
            ]
        verb = "Registered" if repo_command == "add" else "Updated"
        if repo_command == "set-account":
            return [f"Updated {value['repository']}: GitHub account {value['github_account']}"]
        lines = repository_lines(value)
        lines[0] = f"{verb} {lines[0]}"
        return lines
    if command in ("status", "start", "stop"):
        return status_lines(value)
    if command == "scan":
        return [f"Queued scan {identifier}" for identifier in value["scans"]] or [
            "No repositories to scan."
        ]
    if command in ("pause", "resume"):
        return [f"{'Paused' if value['paused'] else 'Resumed'} scans for {value['repository']}"]
    return []


def output(value, args):
    text = (
        json.dumps(value, indent=2)
        if args.json
        else "\n".join(human_lines(value, args.command, getattr(args, "repo_command", None)))
    )
    print(text, flush=True)
