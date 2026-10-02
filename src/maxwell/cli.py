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
from maxwell.config import Paths
from maxwell.daemon import Daemon, request
from maxwell.github import GitHub
from maxwell.repositories import git, github_name, inspect_repository, validate_ref
from maxwell.store import Store


def parser():
    root = argparse.ArgumentParser(prog="maxwell", description="Repository discovery daemon")
    root.add_argument("--version", action="version", version=f"maxwell {__version__}")
    root.add_argument("--home", help="Runtime directory (default: ~/.maxwell)")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create private configuration and runtime directories")
    repo = commands.add_parser("repo", help="Register or list repositories")
    repositories = repo.add_subparsers(dest="repo_command", required=True)
    add = repositories.add_parser("add")
    add.add_argument("source", help="Local path, owner/repository, or GitHub repository URL")
    add.add_argument("--github", help="GitHub owner/repository; inferred from origin if possible")
    add.add_argument("--ref", help="Revision to scan (local: HEAD; remote: GitHub default branch)")
    add.add_argument(
        "--account", default="default", help="GitHub credential profile from config.yaml"
    )
    repositories.add_parser("list")
    account = repositories.add_parser("set-account", help="Change a repository's GitHub profile")
    account.add_argument("repository")
    account.add_argument("--account", required=True)
    start = commands.add_parser("start", help="Start the daemon; also begins scheduled discovery")
    start.add_argument("--foreground", action="store_true")
    commands.add_parser("stop", help="Request graceful shutdown")
    commands.add_parser("status", help="Show health, jobs, spending, and publication failures")
    scan = commands.add_parser("scan", help="Queue a scan even if the revision has not changed")
    scan.add_argument("repository", nargs="?")
    scan.add_argument("--priority", choices=["P0", "P1", "P2", "P3"], default="P2")
    for name in ("pause", "resume"):
        item = commands.add_parser(name)
        item.add_argument("repository", nargs="?")
    logs = commands.add_parser("logs", help="Show daemon logs")
    logs.add_argument("--follow", action="store_true")
    return root


def output(value):
    print(json.dumps(value, indent=2))


def infer_github(path):
    remote = git("remote", "get-url", "origin", cwd=path)
    if not remote.startswith(("https://github.com/", "git@github.com:")):
        raise ValueError("Cannot infer GitHub repository; pass --github owner/repository")
    return github_name(remote)


def register_repository(args, paths, config):
    settings = config.github.for_account(args.account)
    local = Path(args.source).expanduser()
    if local.is_dir():
        ref = args.ref or "HEAD"
        source = inspect_repository(local, ref)
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
        ref = validate_ref(args.ref or metadata["default_branch"])
        source, kind = f"https://github.com/{name}.git", "github"
    store = Store(paths.db)
    try:
        store.register(name, source, ref, kind, args.account)
    finally:
        store.close()
    return {
        "repository": name,
        "source": source,
        "source_kind": kind,
        "ref": ref,
        "github_account": args.account,
    }


def execute(args):
    paths = Paths(args.home)
    if args.command == "init":
        if paths.config.exists():
            raise ValueError("Configuration already exists; it was not overwritten")
        paths.init_config()
        Store(paths.db).close()
        print(f"Created {paths.config}. Set GitHub token, provider API key, model, and prices.")
        return
    config = paths.load()
    paths.initialize()
    if args.command == "repo":
        if args.repo_command == "add":
            output(register_repository(args, paths, config))
            return
        store = Store(paths.db)
        try:
            if args.repo_command == "list":
                output(store.rows("SELECT * FROM repositories ORDER BY name"))
            elif args.repo_command == "set-account":
                config.github.for_account(args.account)
                store.set_account(args.repository, args.account)
                output({"repository": args.repository, "github_account": args.account})
        finally:
            store.close()
    elif args.command == "start":
        config.ready()
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
                output(response)
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
                    output(asyncio.run(request(paths, "status")))
                    return
                except (OSError, TimeoutError):
                    time.sleep(0.1)
            raise ValueError(f"Daemon startup timed out; inspect {paths.log}")
    elif args.command == "logs":
        if not paths.log.exists():
            print("No daemon logs yet")
            return
        with paths.log.open() as log:
            print("".join(log.readlines()[-100:]), end="")
            if args.follow:
                while True:
                    line = log.readline()
                    if line:
                        print(line, end="", flush=True)
                    else:
                        time.sleep(0.2)
    elif args.command == "status":
        try:
            output(asyncio.run(request(paths, "status")))
        except (OSError, TimeoutError):
            store = Store(paths.db)
            try:
                output({"daemon": "stopped", **store.status()})
            finally:
                store.close()
    else:
        arguments = {}
        if hasattr(args, "repository"):
            arguments["repository"] = args.repository
        if args.command == "scan":
            arguments["priority"] = int(args.priority[1])
        response = asyncio.run(request(paths, args.command, **arguments))
        if args.command == "stop":
            deadline = time.monotonic() + config.limits.shutdown_grace_seconds + 20
            while time.monotonic() < deadline and paths.socket.exists():
                time.sleep(0.1)
            if paths.socket.exists():
                raise ValueError("Shutdown still pending; inspect logs (no PID was blindly killed)")
            response = {"daemon": "stopped"}
        output(response)


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
        print(f"maxwell: {message}", file=sys.stderr)
        raise SystemExit(1)
