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


def scan_line(scan):
    name = scan["repository"]
    if scan["state"] == "progress":
        elapsed = scan["elapsed"]
        minutes, seconds = divmod(elapsed, 60)
        badge = "[WAIT]" if scan["heartbeat"] else "[STEP]"
        print(
            f"{color(badge, '36')} {name}: {scan['phase']} ({minutes}:{seconds:02d} elapsed)",
            flush=True,
        )
    elif scan["state"] == "running":
        effort = f" / {scan['effort']}" if scan.get("effort") else ""
        stage = f" / {scan['stage']}" if scan.get("stage") in ("review", "audit") else ""
        print(
            f"{color('[RUN]', '36')} {name} / {scan['provider']} / {scan['model']}{effort}{stage}",
            flush=True,
        )
        if decision := scan.get("agent_selection"):
            method = {
                "explicit": "explicit choice",
                "thompson-source-v1": "sampled using provisional evidence",
                "uniform-unrated-v1": "sampled equally; unrated",
            }[decision["method"]]
            independence = (
                "; different model unavailable"
                if decision["independence"] == "constrained"
                else "; independence overridden"
                if decision["independence"] == "overridden"
                else ""
            )
            print(f"{color('[PICK]', '36')} {name}: {method}{independence}", flush=True)
    elif scan["state"] == "publishing":
        print(f"{color('[PUB]', '36')} {name}: publishing results", flush=True)
    else:
        success = scan["state"] == "completed" and not scan["publication_errors"]
        badge = color("[OK]" if success else "[FAIL]", "32" if success else "31")
        detail = (
            f"{scan['admitted']} new findings"
            if success
            else scan["error"] or "; ".join(scan["publication_errors"]) or scan["state"]
        )
        if success and scan.get("selection"):
            detail += f"; {scan['selection']['skipped']} candidates skipped"
        if success and scan.get("stage") in ("review", "audit"):
            verdicts = ", ".join(item["verdict"] for item in scan.get("assessments", []))
            detail = f"{scan['stage']}: {verdicts or 'no assessment'}"
        print(f"{badge} {name}: {detail}", flush=True)


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
    if command == "scan":
        skipped = [
            (
                f"{item['repository']}: no eligible findings for {item['stage']}."
                if item.get("reason", "No eligible findings") == "No eligible findings"
                else f"[SKIP] {item['repository']} / {item['stage']}: {item['reason']}"
            )
            for item in value.get("skipped", [])
        ]
        if not value["scans"] and not skipped and not value.get("errors"):
            return ["No repositories to scan."]
        lines = [
            f"[FAIL] {item['repository']} / {item['stage']}: {item['error']}"
            for item in value.get("errors", [])
        ] + skipped
        for scan in value["scans"]:
            if scan["report"]:
                lines.append(f"Report: {scan['report']}")
            for finding in scan["findings"]:
                if finding["url"]:
                    lines.append(f"Finding: {finding['url']}")
            for assessment in scan.get("assessments", []):
                lines.append(
                    f"Assessment: https://github.com/{scan['repository']}/issues/{assessment['issue']}"
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
