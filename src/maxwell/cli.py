import argparse
import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx

from maxwell import __version__
from maxwell.cli_help import TreeParser
from maxwell.cli_output import check_line, color, model_lines, output, scan_line
from maxwell.config import Paths
from maxwell.config_apply import apply_config
from maxwell.github import GitHub
from maxwell.manual import run_scans
from maxwell.repositories import (
    git,
    github_name,
    inspect_checkout,
    inspect_repository,
    validate_branch,
    validate_ref,
)
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


def parser():
    root = TreeParser(prog="maxwell", description="Manual repository discovery")
    root.add_argument("--version", action="version", version=f"maxwell {__version__}")
    root.add_argument("--home", help="Runtime directory (default: ~/.maxwell)")
    root.add_argument(
        "--json", action="store_true", help="Print JSON instead of human-readable output"
    )
    commands = root.add_subparsers(dest="command", required=True)
    config = add_command(commands, "config", help="Initialize or apply configuration")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    add_command(config_commands, "init", help="Create private configuration and runtime files")
    add_command(
        config_commands,
        "apply",
        help="Reload settings, discover models, verify access, and save metadata",
    )
    models = add_command(commands, "models", help="Show stored models and efforts")
    model_commands = models.add_subparsers(dest="models_command", required=True)
    model_list = add_command(
        model_commands, "list", help="List stored model metadata and access results"
    )
    model_list.add_argument("--provider", help="List one configured provider/account")
    repo = add_command(commands, "repo", help="Register, change, or list repositories")
    repositories = repo.add_subparsers(dest="repo_command", required=True)
    add = add_command(repositories, "add", help="Register a repository and discovery settings")
    add.add_argument("source", help="Local path, owner/repository, or GitHub repository URL")
    add.add_argument("--github", help="GitHub owner/repository; inferred from origin if possible")
    add.add_argument("--directory", help="Existing checkout to switch and update before scans")
    add.add_argument("--ref", default="main", help="Base branch to scan (default: main)")
    add.add_argument("--provider", help="Configured provider/account for discovery")
    add.add_argument("--model", help="Discovery model (otherwise use the native default)")
    add.add_argument("--effort", help="Reasoning effort (otherwise use the provider default)")
    add.add_argument(
        "--account", default="default", help="GitHub credential profile from config.yaml"
    )
    add_command(repositories, "list", help="Show repository settings and stage routes")
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
    change.add_argument(
        "--stage",
        choices=["discovery", "review", "reproduction", "fix", "validation"],
        help="Stage whose provider/model settings should change",
    )
    change.add_argument("--provider", help="Configured provider/account for the stage")
    change.add_argument("--model", help="Model for the stage")
    change.add_argument("--effort", help="Stage effort; use 'default' to reset")
    scan = add_command(commands, "scan", help="Run discovery now and publish findings")
    scan.add_argument("repository", nargs="?")
    scan.add_argument("--priority", choices=["P0", "P1", "P2", "P3"], default="P2")
    scan.add_argument("--model", required=True, help="Model to use for this manual discovery run")
    scan.add_argument(
        "--effort", help="Reasoning effort for this run; 'default' uses the provider default"
    )
    scan.add_argument(
        "--provider", help="Override the repository's saved discovery provider/account"
    )
    return root


def infer_github(path):
    remote = git("remote", "get-url", "origin", cwd=path)
    if not remote.startswith(("https://github.com/", "git@github.com:")):
        raise ValueError("Cannot infer GitHub repository; pass --github owner/repository")
    return github_name(remote)


def register_repository(args, paths, config):
    route = config.route(args.provider, args.model, effort=args.effort)
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
        store.register(name, source, ref, kind, args.account, directory, route)
    finally:
        store.close()
    return {
        "repository": name,
        "source": source,
        "source_kind": kind,
        "ref": ref,
        "github_account": args.account,
        "directory": directory,
        "stages": {"discovery": route},
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
    route = None
    stage = args.stage or "discovery"
    if args.provider is not None or args.model is not None or args.effort is not None:
        route = config.route(
            args.provider, args.model, store.saved_route(record["name"], stage), effort=args.effort
        )
    elif args.stage is not None:
        raise ValueError("Specify --provider, --model, or --effort when changing a stage")
    if not changes and route is None:
        raise ValueError("Specify repository settings or --provider/--model/--effort")
    updated = {**record, **changes}
    if updated["directory"] is not None:
        validate_branch(updated["ref"])
    elif "ref" in changes and updated["source_kind"] == "local":
        inspect_repository(updated["source"], updated["ref"])
    return store.change_repository(record["name"], changes, stage, route)


def execute(args):
    def emit(value):
        output(value, args)

    paths = Paths(args.home)
    if args.command == "config" and args.config_command == "apply":
        if not args.json:
            print("Applying config...", flush=True)
        report = apply_config(paths, on_check=None if args.json else check_line)
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
    elif args.command == "scan":
        report = asyncio.run(
            run_scans(
                paths,
                config,
                args.repository,
                args.model,
                provider=args.provider,
                effort=args.effort,
                priority=int(args.priority[1]),
                on_progress=None if args.json else scan_line,
            )
        )
        emit(report)
        if not report["valid"]:
            raise SystemExit(1)


def main():
    cli = parser()
    if len(sys.argv) == 1:
        cli.print_help()
        return
    args = cli.parse_args()
    try:
        execute(args)
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
