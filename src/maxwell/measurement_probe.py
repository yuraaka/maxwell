import hashlib
import importlib.metadata
import json
import os
import platform
import sys
from pathlib import Path

import pytest

collected = []
outcomes = {}
durations = {}
collection_errors = 0


def pytest_addoption(parser):
    parser.addoption("--maxwell-measure-report", required=True)


def pytest_collection_finish(session):
    collected.extend(item.nodeid for item in session.items)


def pytest_collectreport(report):
    global collection_errors
    collection_errors += report.failed


def pytest_runtest_logreport(report):
    durations[report.nodeid] = durations.get(report.nodeid, 0) + report.duration
    current = outcomes.get(report.nodeid)
    if report.failed:
        outcomes[report.nodeid] = "failed"
    elif current != "failed" and (report.skipped or hasattr(report, "wasxfail")):
        outcomes[report.nodeid] = "skipped"
    elif report.when == "call" and current not in ("failed", "skipped"):
        outcomes[report.nodeid] = report.outcome


def pytest_sessionfinish(session, exitstatus):
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
