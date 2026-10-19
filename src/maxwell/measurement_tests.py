import asyncio
import contextlib
import hashlib
import json
import os
import platform
import shutil
import signal
import statistics
import time
from pathlib import Path

from maxwell.measurement import digest
from maxwell.records import MeasurementFactor
from maxwell.repositories import git


async def finish_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 1)
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def prepare_snapshot(repository, identity, token, sha):
    preparation = asyncio.create_task(
        asyncio.to_thread(repository.prepare, identity, token, revision=sha)
    )
    try:
        return await asyncio.shield(preparation)
    except asyncio.CancelledError:
        try:
            _, worktree = await preparation
            await asyncio.to_thread(repository.cleanup, worktree)
        finally:
            raise


async def run_test_measurement(
    repository, identity, token, sha, profile, artifact, progress, source_roots
):
    probe = artifact / "probe"
    probe.mkdir()
    shutil.copyfile(
        Path(__file__).with_name("measurement_probe.py"), probe / "_maxwell_measurement_probe.py"
    )
    runs, invalid, fingerprint = [], [], None
    total_runs = profile.warmups + profile.runs
    for index in range(profile.warmups + profile.runs):
        label = f"test run {index + 1}/{total_runs}"
        progress({"phase": label, "stage_fraction": (1 + index) / (total_runs + 2)})
        _, worktree = await prepare_snapshot(repository, f"{identity}-test-{index}", token, sha)
        process = None
        monitoring = None
        run_dir = artifact / f"test-{index}"
        run_dir.mkdir()
        home = run_dir / "home"
        home.mkdir()
        report_path = run_dir / "report.json"
        progress_path = run_dir / "progress.json"
        last_snapshot = None

        def read_progress():
            nonlocal last_snapshot
            try:
                data = json.loads(progress_path.read_text())
                if data == last_snapshot:
                    return
                completed, total = data["completed"], data["total"]
                if not (
                    isinstance(completed, int)
                    and isinstance(total, int)
                    and 0 <= completed <= total
                    and total > 0
                ):
                    return
                failed, current = data["failed"], data["current"]
                if not isinstance(failed, int) or failed < 0 or not isinstance(current, str):
                    return
                last_snapshot = data
                phase = f"{label} · {completed}/{total} tests · {failed} failed"
                if current:
                    phase += f" · {current[:500]}"
                progress(
                    {
                        "phase": phase,
                        "stage_fraction": (1 + index + completed / total) / (total_runs + 2),
                    }
                )
            except (OSError, ValueError, KeyError, TypeError):
                pass

        async def monitor():
            while True:
                read_progress()
                await asyncio.sleep(0.25)

        environment = {
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "HOME": str(home),
            "TMPDIR": str(run_dir),
            "PYTHONHASHSEED": "0",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            **profile.environment,
            "PYTHONPATH": os.pathsep.join(
                [str(probe), *(str(worktree / root) for root in source_roots)]
            )
            + (
                os.pathsep + profile.environment["PYTHONPATH"]
                if profile.environment.get("PYTHONPATH")
                else ""
            ),
            "MAXWELL_MEASUREMENT_REPORT": str(report_path),
        }
        cwd = worktree / profile.cwd
        try:
            if not cwd.resolve().is_relative_to(worktree.resolve()):
                raise ValueError("Test cwd escapes snapshot")
            start = time.monotonic()
            with (run_dir / "output.log").open("wb") as log:
                launch = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        *profile.command,
                        "-p",
                        "_maxwell_measurement_probe",
                        "--maxwell-measure-report",
                        str(report_path),
                        cwd=cwd,
                        env=environment,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                    )
                )
                try:
                    process = await asyncio.shield(launch)
                except asyncio.CancelledError:
                    process = await launch
                    raise
                (artifact / "process.json").write_text(
                    json.dumps({"pid": process.pid, "report": str(report_path)})
                )
                monitoring = asyncio.create_task(monitor())
                try:
                    await asyncio.wait_for(process.wait(), profile.timeout_seconds)
                except TimeoutError:
                    invalid.append(f"Run {index + 1}: timeout after {profile.timeout_seconds:g}s")
                    continue
            duration = time.monotonic() - start
            read_progress()
            if git("rev-parse", "HEAD", cwd=worktree) != sha or git(
                "status", "--porcelain", "--untracked-files=no", cwd=worktree
            ):
                invalid.append(f"Run {index + 1}: tracked source changed")
                continue
            if not report_path.exists():
                invalid.append(f"Run {index + 1}: pytest report missing")
                continue
            data = json.loads(report_path.read_text())
            if (
                process.returncode not in (0, 1)
                or data["exitstatus"] != process.returncode
                or data["collection_errors"]
                or not data["collected"]
                or set(data["collected"]) != set(data["outcomes"])
            ):
                invalid.append(f"Run {index + 1}: incomplete collection or execution")
                continue
            locks = {
                path: hashlib.sha256((worktree / path).read_bytes()).hexdigest()
                for path in ("uv.lock", "poetry.lock", "requirements.txt", "pyproject.toml")
                if (worktree / path).is_file() and not (worktree / path).is_symlink()
            }
            current = digest(
                {
                    "environment": data["environment"],
                    "configured_environment": profile.environment,
                    "command": profile.command,
                    "cwd": profile.cwd,
                    "locks": locks,
                    "host": platform.node(),
                }
            )
            if fingerprint is not None and current != fingerprint:
                invalid.append(f"Run {index + 1}: environment changed")
                continue
            fingerprint = current
            if index >= profile.warmups:
                runs.append({**data, "duration_seconds": duration})
        except (OSError, ValueError, KeyError, TypeError):
            invalid.append(f"Run {index + 1}: runner or report error")
        finally:
            if monitoring is not None:
                monitoring.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitoring
            if process is not None:
                await finish_group(process)
            await asyncio.to_thread(repository.cleanup, worktree)
    details = {
        "requested_runs": profile.runs,
        "warmups": profile.warmups,
        "valid_runs": len(runs),
        "invalid_runs": invalid,
        "runner": Path(profile.command[0]).name,
        "command_fingerprint": digest(profile.command),
        "cwd": profile.cwd,
    }
    factors, status = {}, {}
    if len(runs) != profile.runs or invalid:
        reason = "Repeated test execution incomplete: " + "; ".join(invalid[:3])
        if any("timeout after" in failure for failure in invalid):
            reason += "; increase measure.tests.<owner/repo>.timeout_seconds for longer suites"
        return (
            factors,
            dict.fromkeys(("test_runtime", "test_flakiness"), reason),
            details,
            fingerprint,
        )
    durations = [run["duration_seconds"] for run in runs]
    median = statistics.median(durations)
    details.update(duration_seconds=durations, median_seconds=median)
    if profile.runtime_budget_seconds:
        factors["test_runtime"] = MeasurementFactor(
            numerator=median,
            denominator=profile.runtime_budget_seconds,
            unit="seconds/budget_seconds",
        )
    else:
        status["test_runtime"] = "Runtime budget not configured"
    collected = [set(run["collected"]) for run in runs]
    if any(items != collected[0] for items in collected):
        status["test_flakiness"] = "Collected test IDs changed between runs"
        status["test_runtime"] = "Collected test IDs changed between runs"
        factors.pop("test_runtime", None)
    else:
        outcomes = {test: {run["outcomes"][test] for run in runs} for test in collected[0]}
        eligible = {
            test: values for test, values in outcomes.items() if values <= {"passed", "failed"}
        }
        flaky = sorted(test for test, values in eligible.items() if len(values) > 1)
        persistent = sorted(test for test, values in eligible.items() if values == {"failed"})
        details.update(
            collected_tests=len(outcomes),
            observed_tests=len(eligible),
            skipped_tests=len(outcomes) - len(eligible),
            observed_flaky_count=len(flaky),
            observed_flaky_tests=flaky[:50],
            persistent_failure_count=len(persistent),
            persistent_failures=persistent[:50],
        )
        if eligible:
            factors["test_flakiness"] = MeasurementFactor(
                numerator=len(flaky), denominator=len(eligible), unit="observed_flaky_tests/tests"
            )
        else:
            status["test_flakiness"] = "No consistently executed pass/fail tests"
    details["slow_tests"] = sorted(
        (
            {
                "test": test,
                "median_seconds": statistics.median(run["durations"].get(test, 0) for run in runs),
            }
            for test in collected[0]
        ),
        key=lambda item: (-item["median_seconds"], item["test"]),
    )[:20]
    return factors, status, details, fingerprint
