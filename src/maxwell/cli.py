import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import argcomplete
import httpx

from maxwell import __version__
from maxwell.cli_help import TreeParser
from maxwell.cli_output import check_line, color, measurement_line, model_lines, output
from maxwell.completion import bash_completion, configure_completion
from maxwell.config import Paths
from maxwell.config_apply import apply_config
from maxwell.github import GitHub
from maxwell.manual import run_scans
from maxwell.measurement_runner import run_measurements
from maxwell.repositories import (
    git,
    github_name,
    inspect_checkout,
    inspect_repository,
    validate_branch,
    validate_ref,
)
from maxwell.scan_display import ScanDisplay
from maxwell.stages import SKIPPABLE_STAGES, select_stages
from maxwell.store import Store


def add_command(commands, name, **options):
    command = commands.add_parser(name, **options)
    command.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print JSON instead of human-readable output",
    )
    return command


def stage_argument(value):
    try:
        select_stages(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Use a positive integer")
    return number


def parser():
    root = TreeParser(
        prog="maxwell",
        description="Repository maintenance: hunt, validate, audit, fix, review, integrate",
    )
    root.add_argument("--version", action="version", version=f"maxwell {__version__}")
    root.add_argument("--home", help="Runtime directory (default: ~/.maxwell)")
    root.add_argument(
        "--json", action="store_true", help="Print JSON instead of human-readable output"
    )
    commands = root.add_subparsers(dest="command", required=True)
    measure = add_command(commands, "measure", help="Measure source and test maintenance factors")
    measure.add_argument("repository", nargs="?")
    measure.add_argument("--revision", help="Pinned commit SHA (default: registered branch)")
    measure.add_argument(
        "--skip",
        action="append",
        choices=["source", "tests"],
        default=[],
        help="Skip a stage and reuse its latest compatible GitHub measurement (repeatable)",
    )
    measure.add_argument("--tests", action="store_true", help=argparse.SUPPRESS)
    completion = add_command(commands, "completion", help="Generate shell completion")
    completion.add_argument("shell", choices=["bash"], nargs="?", default="bash")
    config = add_command(commands, "config", help="Initialize or apply configuration")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    add_command(config_commands, "init", help="Create private configuration and runtime files")
    apply = add_command(
        config_commands,
        "apply",
        help="Reload settings and check models for providers without saved data",
    )
    refresh = apply.add_mutually_exclusive_group()
    refresh.add_argument(
        "--force",
        action="store_true",
        help="Rediscover and validate every provider's models, replacing saved checks",
    )
    refresh.add_argument(
        "--skip-model-checks",
        action="store_true",
        help="Reuse saved model metadata without model discovery or access probes",
    )
    models = add_command(commands, "models", help="Show stored models and efforts")
    model_commands = models.add_subparsers(dest="models_command", required=True)
    model_list = add_command(
        model_commands, "list", help="List stored model metadata and access results"
    )
    model_list.add_argument("--provider", help="List one configured provider/account")
    repo = add_command(commands, "repo", help="Register, change, or list repositories")
    repositories = repo.add_subparsers(dest="repo_command", required=True)
    add = add_command(repositories, "add", help="Register a repository")
    add.add_argument("source", help="Local path, owner/repository, or GitHub repository URL")
    add.add_argument("--github", help="GitHub owner/repository; inferred from origin if possible")
    add.add_argument("--directory", help="Existing checkout to switch and update before scans")
    add.add_argument("--ref", default="main", help="Base branch to scan (default: main)")
    add.add_argument(
        "--account", default="default", help="GitHub credential profile from config.yaml"
    )
    add_command(repositories, "list", help="Show repository settings")
    change = add_command(repositories, "change", help="Change a repository's settings")
    change.add_argument("repository")
    directory = change.add_mutually_exclusive_group()
    directory.add_argument(
        "--directory", help="Existing checkout to switch and update before scans"
    )
    directory.add_argument(
        "--no-directory", action="store_true", help="Stop maintaining a checkout"
    )
    change.add_argument("--ref", help="Base branch to scan")
    change.add_argument("--account", help="GitHub credential profile from config.yaml")
    scan = add_command(
        commands, "scan", help="Run the full pipeline, one stage, or a stage interval"
    )
    scan.add_argument("repository", nargs="?")
    pipeline = scan.add_mutually_exclusive_group()
    pipeline.add_argument(
        "--drain",
        action="store_true",
        help="Repeat selected stages until no further progress is possible",
    )
    pipeline.add_argument(
        "--infinite",
        action="store_true",
        help="Repeat selected stages continuously within configured stage pool sizes",
    )
    scan.add_argument(
        "--stage",
        type=stage_argument,
        metavar="STAGE[-STAGE]",
        help="Select hunt, validate, audit, fix, review, integrate; START-END is inclusive, START- runs to the end",
    )
    scan.add_argument(
        "--skip",
        action="append",
        default=[],
        choices=SKIPPABLE_STAGES,
        help="Disable a stage; source/tests reuse compatible GitHub history (repeatable)",
    )
    scan.add_argument(
        "--issue",
        type=positive_int,
        help="Finding issue for validate, audit, fix, review, or integrate",
    )
    scan.add_argument(
        "--limit",
        type=positive_int,
        help="Override the stage's configured assessment limit per repository",
    )
    scan.add_argument(
        "--force",
        action="store_true",
        help="Reassess a finding, retry fix, or supersede its PR review",
    )
    scan.add_argument("--priority", choices=["P0", "P1", "P2", "P3"], default="P2")
    scan.add_argument("--model", help="Pin a model (default: automatic sampling)")
    scan.add_argument(
        "--effort", help="Reasoning effort for this run; 'default' uses the provider default"
    )
    scan.add_argument("--provider", help="Constrain selection to this connection")
    configure_completion(root)
    return root


def infer_github(path):
    remote = git("remote", "get-url", "origin", cwd=path)
    if not remote.startswith(("https://github.com/", "git@github.com:")):
        raise ValueError("Cannot infer GitHub repository; pass --github owner/repository")
    return github_name(remote)


def register_repository(args, paths, config):
    settings = config.github.for_account(args.account)
    local = Path(args.source).expanduser()
    ref = validate_ref(args.ref)
    if local.is_dir():
        source = inspect_repository(local, "HEAD" if args.directory else ref)
        name = github_name(args.github) if args.github else infer_github(source)
        kind = "local"
    else:
        name = github_name(args.source)
        if args.github and github_name(args.github).casefold() != name.casefold():
            raise ValueError("Remote repository and --github must identify the same repository")
        client = GitHub(settings)
        try:
            metadata = client.request("GET", f"repos/{name}")
        finally:
            client.close()
        name = github_name(metadata["full_name"])
        source, kind = f"https://github.com/{name}.git", "github"
    directory = None
    if args.directory is not None:
        validate_branch(ref)
        directory = inspect_checkout(args.directory, name)
        source, kind = f"https://github.com/{name}.git", "github"
    store = Store(paths.db)
    try:
        store.register(name, source, ref, kind, args.account, directory)
    finally:
        store.close()
    return {
        "repository": name,
        "source": source,
        "source_kind": kind,
        "ref": ref,
        "github_account": args.account,
        "directory": directory,
    }


def change_repository(args, store, config):
    record = store.one("SELECT * FROM repositories WHERE name=?", (args.repository,))
    if record is None:
        raise ValueError("Unknown repository")
    changes = {}
    if args.account is not None:
        config.github.for_account(args.account)
        changes["github_account"] = args.account
    if args.ref is not None:
        changes["ref"] = validate_ref(args.ref)
    if args.directory is not None:
        changes.update(
            directory=inspect_checkout(args.directory, record["name"]),
            source=f"https://github.com/{record['name']}.git",
            source_kind="github",
        )
    elif args.no_directory:
        changes["directory"] = None
    if not changes:
        raise ValueError("Specify --directory, --no-directory, --ref, or --account")
    updated = {**record, **changes}
    if updated["directory"] is not None:
        validate_branch(updated["ref"])
    elif "ref" in changes and updated["source_kind"] == "local":
        inspect_repository(updated["source"], updated["ref"])
    return store.change_repository(record["name"], changes)


def execute(args):
    def emit(value):
        output(value, args)

    if args.command == "completion":
        script = bash_completion()
        if args.json:
            print(json.dumps({"shell": args.shell, "script": script}))
        else:
            print(script, end="")
        return
    paths = Paths(args.home)
    if args.command == "config" and args.config_command == "apply":
        if not args.json:
            print("Applying config...", flush=True)
        report = apply_config(
            paths,
            on_check=None if args.json else check_line,
            force=args.force,
            skip_model_checks=args.skip_model_checks,
        )
        emit(report)
        if not report["valid"]:
            raise SystemExit(1)
        return
    if args.command == "config" and args.config_command == "init":
        if paths.config.exists():
            raise ValueError("Configuration already exists; it was not overwritten")
        paths.init_config()
        Store(paths.db).close()
        emit({"config": str(paths.config)})
        return
    if args.command == "models":
        report = Store.cached_models(paths.db, args.provider)
        if args.json:
            emit(report)
        else:
            print("\n".join(model_lines(report)), flush=True)
        return
    config = paths.load()
    paths.initialize()
    if args.command == "repo":
        if args.repo_command == "add":
            if not args.json:
                print("Registering repository...", flush=True)
            emit(register_repository(args, paths, config))
            return
        store = Store(paths.db)
        try:
            if args.repo_command == "list":
                emit(
                    [
                        store.repository(row["name"])
                        for row in store.rows("SELECT name FROM repositories ORDER BY name")
                    ]
                )
            elif args.repo_command == "change":
                emit(change_repository(args, store, config))
        finally:
            store.close()
    elif args.command == "measure":
        report = asyncio.run(
            run_measurements(
                paths,
                config,
                args.repository,
                revision=args.revision,
                run_tests="tests" not in args.skip,
                skip_source="source" in args.skip,
                on_progress=None if args.json else measurement_line,
            )
        )
        if args.json:
            emit(report)
        else:
            for item in report["measurements"]:
                if item["state"] == "completed":
                    score = (
                        f"{item['score']:.2f}/100"
                        if item["score"] is not None
                        else "unknown (incomplete evidence)"
                    )
                    print(
                        f"[OK] {item['repository']}: entropy {score}; factor weight {item['coverage']:.0%}",
                        flush=True,
                    )
                    print(f"Report: {item['report']}", flush=True)
                    print(f"Status: {item['url']}", flush=True)
                    if item["publication_error"]:
                        print(f"[FAIL] publication: {item['publication_error']}", flush=True)
                else:
                    print(f"[FAIL] {item['repository']}: {item['error']}", flush=True)
        if not report["valid"]:
            raise SystemExit(1)
    elif args.command == "scan":
        display = None if args.json else ScanDisplay()
        try:
            report = asyncio.run(
                run_scans(
                    paths,
                    config,
                    args.repository,
                    args.model,
                    provider=args.provider,
                    effort=args.effort,
                    priority=int(args.priority[1]),
                    stage=args.stage,
                    issue=args.issue,
                    limit=args.limit,
                    force=args.force,
                    skip_stages=args.skip,
                    drain=args.drain,
                    infinite=args.infinite,
                    on_progress=display,
                )
            )
        finally:
            if display:
                display.finish()
        emit(report)
        if not report["valid"]:
            raise SystemExit(1)


def main():
    cli = parser()
    argcomplete.autocomplete(cli)
    if len(sys.argv) == 1:
        cli.print_help()
        return
    args = cli.parse_args()
    try:
        execute(args)
        sys.stdout.flush()
    except BrokenPipeError:
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except asyncio.CancelledError:
        raise SystemExit(143)
    except (
        ValueError,
        OSError,
        sqlite3.Error,
        TimeoutError,
        subprocess.SubprocessError,
        httpx.HTTPError,
    ) as exc:
        message = str(exc)
        try:
            message = Paths(args.home).load().redact(message)
        except ValueError:
            pass
        print(
            json.dumps({"error": message})
            if args.json
            else f"{color('maxwell:', '31', sys.stderr)} {message}",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1)
