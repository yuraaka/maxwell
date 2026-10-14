import hashlib
import importlib.metadata
import json
import os
import platform
import sys
import time
from pathlib import Path

import pytest

collected = []
outcomes = {}
durations = {}
collection_errors = 0
progress_path = None
completed_tests = 0
current_test = ""
last_progress = 0.0


def pytest_addoption(parser):
    parser.addoption("--maxwell-measure-report", required=True)


def publish_progress(*, force=False):
    global last_progress
    now = time.monotonic()
    if progress_path is None or (not force and now - last_progress < 0.25):
        return
    last_progress = now
    try:
        temporary = progress_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "total": len(collected),
                    "completed": completed_tests,
                    "failed": sum(value == "failed" for value in outcomes.values()),
                    "current": current_test[:500],
                }
            )
        )
        temporary.replace(progress_path)
    except OSError:
        pass


def pytest_collection_finish(session):
    global progress_path
    collected.extend(item.nodeid for item in session.items)
    progress_path = Path(session.config.getoption("--maxwell-measure-report")).with_name(
        "progress.json"
    )
    publish_progress(force=True)


def pytest_runtest_logstart(nodeid, location):
    global current_test
    current_test = nodeid
    publish_progress(force=True)


def pytest_collectreport(report):
    global collection_errors
    collection_errors += report.failed


def pytest_runtest_logreport(report):
    global completed_tests, current_test
    durations[report.nodeid] = durations.get(report.nodeid, 0) + report.duration
    current = outcomes.get(report.nodeid)
    if report.failed:
        outcomes[report.nodeid] = "failed"
    elif current != "failed" and (report.skipped or hasattr(report, "wasxfail")):
        outcomes[report.nodeid] = "skipped"
    elif report.when == "call" and current not in ("failed", "skipped"):
        outcomes[report.nodeid] = report.outcome

    if report.when == "teardown":
        completed_tests += 1
        current_test = ""
        publish_progress()


def pytest_sessionfinish(session, exitstatus):
    publish_progress(force=True)
    packages = sorted(
        (item.metadata.get("Name", ""), item.version) for item in importlib.metadata.distributions()
    )
    environment = {
        "python": sys.version,
        "pytest": pytest.__version__,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "packages": hashlib.sha256(json.dumps(packages).encode()).hexdigest(),
    }
    Path(session.config.getoption("--maxwell-measure-report")).write_text(
        json.dumps(
            {
                "collected": collected,
                "outcomes": outcomes,
                "durations": durations,
                "collection_errors": collection_errors,
                "exitstatus": int(exitstatus),
                "environment": environment,
            }
        )
    )
