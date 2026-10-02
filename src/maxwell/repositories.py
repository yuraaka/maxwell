import asyncio
import base64
import hashlib
import os
import re
import signal
import subprocess
from pathlib import Path


def git(*args, cwd=None, token=None):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_TERMINAL_PROMPT"] = "0"
    encoded = None
    if token is not None:
        if not token:
            raise ValueError("A GitHub token is required for remote fetching")
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        config = {
            "credential.helper": "",
            "http.extraHeader": "",
            "http.https://github.com/.extraHeader": f"Authorization: Basic {encoded}",
            "http.followRedirects": "false",
            "protocol.allow": "never",
            "protocol.https.allow": "always",
        }
        environment.update(
            {
                "GIT_ASKPASS": "/usr/bin/false",
                "SSH_ASKPASS": "/usr/bin/false",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_COUNT": str(len(config)),
            }
        )
        for index, (key, value) in enumerate(config.items()):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = value
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    if result.returncode:
        error = result.stderr.strip()
        if token:
            error = error.replace(token, "[REDACTED]").replace(encoded, "[REDACTED]")
        raise ValueError(f"Git operation failed: {error[:1000]}")
    return result.stdout.strip()


def github_name(value):
    if value.startswith("https://github.com/"):
        value = value.removeprefix("https://github.com/")
    elif value.startswith("git@github.com:"):
        value = value.removeprefix("git@github.com:")
    value = value.removesuffix("/").removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", value) or value.split("/")[-1] in (
        ".",
        "..",
    ):
        raise ValueError(
            "Use a local repository path, owner/repository, or a github.com repository URL"
        )
    return value


def validate_ref(ref):
    if ref.startswith("-") or not ref or "\n" in ref or "\r" in ref:
        raise ValueError("Invalid Git revision")
    return ref


def inspect_repository(path, ref):
    source = Path(path).expanduser().resolve()
    if not source.is_dir():
        raise ValueError("Repository path is not a directory")
    git("rev-parse", "--git-dir", cwd=source)
    validate_ref(ref)
    git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=source)
    return str(source)


class Repository:
    def __init__(self, paths, record):
        self.record = record
        key = hashlib.sha256(record["name"].encode()).hexdigest()[:16]
        self.directory = paths.root / "repos" / key
        self.mirror = self.directory / "mirror.git"
        self.worktrees = self.directory / "worktrees"

    def prepare(self, scan_id, token=None):
        self.worktrees.mkdir(parents=True, exist_ok=True)
        if not self.mirror.exists():
            git("init", "--bare", str(self.mirror))
        remote = self.record.get("source_kind", "local") == "github"
        source = self.record["source"]
        if remote and source != f"https://github.com/{github_name(self.record['name'])}.git":
            raise ValueError("Remote source does not match the registered GitHub repository")
        git(
            "-C",
            str(self.mirror),
            "fetch",
            "--no-tags",
            "--",
            source,
            self.record["ref"],
            token=token if remote else None,
        )
        sha = git("-C", str(self.mirror), "rev-parse", "FETCH_HEAD^{commit}")
        worktree = self.worktrees / scan_id
        git("-C", str(self.mirror), "worktree", "add", "--detach", str(worktree), sha)
        return sha, worktree

    def unchanged(self, worktree, sha):
        return git("rev-parse", "HEAD", cwd=worktree) == sha and not git(
            "status", "--porcelain", "--untracked-files=all", "--ignored", cwd=worktree
        )

    def cleanup(self, worktree):
        if worktree and worktree.parent == self.worktrees and worktree.exists():
            git("-C", str(self.mirror), "worktree", "remove", "--force", str(worktree))


async def terminate(process, grace=2):
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), grace)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


def stop_orphan(pid, artifact):
    if not pid or not artifact:
        return
    command = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "args="], capture_output=True, text=True, timeout=5
    ).stdout
    marker = str(Path(artifact) / "result.json")
    if marker not in command:
        return
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
