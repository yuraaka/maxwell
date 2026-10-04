import argparse
import asyncio
import json
import logging
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

from maxwell import __version__
from maxwell.cli_output import check_line, color, output
from maxwell.config import Paths
from maxwell.config_check import check_config
from maxwell.daemon import Daemon, request
from maxwell.github import GitHub
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
    root = argparse.ArgumentParser(prog="maxwell", description="Repository discovery daemon")
    root.add_argument("--version", action="version", version=f"maxwell {__version__}")
    root.add_argument("--home", help="Runtime directory (default: ~/.maxwell)")
    root.add_argument(
        "--json", action="store_true", help="Print JSON instead of human-readable output"
    )
    commands = root.add_subparsers(dest="command", required=True)
    add_command(commands, "init", help="Create private configuration and runtime directories")
    config = add_command(commands, "config", help="Check configuration")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    check = add_command(
        config_commands, "check", help="Validate settings, GitHub, and model access"
    )
    check.add_argument("--offline", action="store_true", help="Skip GitHub and provider requests")
    repo = add_command(commands, "repo", help="Register, change, or list repositories")
    repositories = repo.add_subparsers(dest="repo_command", required=True)
    add = add_command(repositories, "add")
    add.add_argument("source", help="Local path, owner/repository, or GitHub repository URL")
    add.add_argument("--github", help="GitHub owner/repository; inferred from origin if possible")
    add.add_argument("--directory", help="Existing checkout to switch and update before scans")
    add.add_argument("--ref", default="main", help="Base branch to scan (default: main)")
    add.add_argument("--provider", help="Configured provider/account for discovery")
    add.add_argument("--model", help="Discovery model (otherwise use the native default)")
    add.add_argument(
        "--account", default="default", help="GitHub credential profile from config.yaml"
    )
    add_command(repositories, "list")
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
    account = add_command(repositories, "set-account", help="Change a repository's GitHub profile")
    account.add_argument("repository")
    account.add_argument("--account", required=True)
    start = add_command(commands, "start", help="Start the daemon; also begins scheduled discovery")
    start.add_argument("--foreground", action="store_true")
    add_command(commands, "stop", help="Request graceful shutdown")
    add_command(commands, "status", help="Show health, jobs, spending, and publication failures")
    scan = add_command(commands, "scan", help="Queue a scan even if the revision has not changed")
    scan.add_argument("repository", nargs="?")
    scan.add_argument("--priority", choices=["P0", "P1", "P2", "P3"], default="P2")
    scan.add_argument("--model", required=True, help="Model to use for this manual discovery run")
    scan.add_argument(
        "--provider", help="Override the repository's saved discovery provider/account"
    )
    for name in ("pause", "resume"):
        item = add_command(commands, name)
        item.add_argument("repository", nargs="?")
    logs = add_command(commands, "logs", help="Show daemon logs")
    logs.add_argument("--follow", action="store_true")
    return root


def infer_github(path):
    remote = git("remote", "get-url", "origin", cwd=path)
    if not remote.startswith(("https://github.com/", "git@github.com:")):
        raise ValueError("Cannot infer GitHub repository; pass --github owner/repository")
    return github_name(remote)


def register_repository(args, paths, config):
    route = config.route(args.provider, args.model)
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
    if args.provider is not None or args.model is not None:
        route = config.route(args.provider, args.model, store.saved_route(record["name"], stage))
    elif args.stage is not None:
        raise ValueError("Specify --provider or --model when changing a stage")
    if not changes and route is None:
        raise ValueError("Specify repository settings or --provider/--model")
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
    if args.command == "config":
        if not args.json:
            print(f"Checking config ({'offline' if args.offline else 'online'})...", flush=True)
        report = check_config(
            paths, online=not args.offline, on_check=None if args.json else check_line
        )
        emit(report)
        if not report["valid"]:
            raise SystemExit(1)
        return
    if args.command == "init":
        if paths.config.exists():
            raise ValueError("Configuration already exists; it was not overwritten")
        paths.init_config()
        Store(paths.db).close()
        emit({"config": str(paths.config)})
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
            elif args.repo_command == "set-account":
                config.github.for_account(args.account)
                store.set_account(args.repository, args.account)
                emit({"repository": args.repository, "github_account": args.account})
        finally:
            store.close()
    elif args.command == "start":
        config.ready()
        if not args.json:
            print("Starting daemon...", flush=True)
        if args.foreground:
            logging.basicConfig(
                filename=paths.log,
                level=logging.INFO,
                format="%(asctime)s %(levelname)s %(message)s",
            )
            asyncio.run(Daemon(paths, config).serve())
        else:
            try:
                response = asyncio.run(request(paths, "status"))
                emit(response)
                return
            except (OSError, TimeoutError):
                pass
            with paths.log.open("a") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "maxwell",
                        "--home",
                        str(paths.root),
                        "start",
                        "--foreground",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    cwd=paths.root,
                )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise ValueError(f"Daemon failed to start; inspect {paths.log}")
                try:
                    emit(asyncio.run(request(paths, "status")))
                    return
                except (OSError, TimeoutError):
                    time.sleep(0.1)
            raise ValueError(f"Daemon startup timed out; inspect {paths.log}")
    elif args.command == "logs":
        if not paths.log.exists():
            print(json.dumps({"logs": []}) if args.json else "No daemon logs yet", flush=True)
            return
        with paths.log.open() as log:
            lines = log.readlines()[-100:]
            if args.json and not args.follow:
                print(
                    json.dumps({"logs": [line.rstrip("\n") for line in lines]}, indent=2),
                    flush=True,
                )
            elif args.json:
                for line in lines:
                    print(json.dumps({"line": line.rstrip("\n")}), flush=True)
            else:
                print("".join(lines), end="", flush=True)
            if args.follow:
                while True:
                    line = log.readline()
                    if line:
                        if args.json:
                            print(json.dumps({"line": line.rstrip("\n")}), flush=True)
                        else:
                            print(line, end="", flush=True)
                    else:
                        time.sleep(0.2)
    elif args.command == "status":
        try:
            emit(asyncio.run(request(paths, "status")))
        except (OSError, TimeoutError):
            store = Store(paths.db)
            try:
                emit({"daemon": "stopped", **store.status()})
            finally:
                store.close()
    else:
        if args.command == "stop" and not args.json:
            print("Stopping daemon...", flush=True)
        arguments = {}
        if hasattr(args, "repository"):
            arguments["repository"] = args.repository
        if args.command == "scan":
            arguments["priority"] = int(args.priority[1])
            arguments["model"] = args.model
            arguments["provider"] = args.provider
        response = asyncio.run(request(paths, args.command, **arguments))
        if args.command == "stop":
            deadline = time.monotonic() + config.limits.shutdown_grace_seconds + 20
            while time.monotonic() < deadline and paths.socket.exists():
                time.sleep(0.1)
            if paths.socket.exists():
                raise ValueError("Shutdown still pending; inspect logs (no PID was blindly killed)")
            response = {"daemon": "stopped"}
        emit(response)


def main():
    args = parser().parse_args()
    try:
        execute(args)
    except KeyboardInterrupt:
        raise SystemExit(130)
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
