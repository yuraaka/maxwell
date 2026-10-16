import asyncio
import base64
import hashlib
import os
import re
import signal
import subprocess
from pathlib import Path


class RepairMergeConflict(ValueError):
    def __init__(self, paths):
        self.paths = paths
        super().__init__("Repair conflicts with target base in: " + ", ".join(paths))


def git(*args, cwd=None, token=None, strip=True):
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
    return result.stdout.strip() if strip else result.stdout


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


def validate_branch(ref):
    validate_ref(ref)
    if ref == "HEAD":
        raise ValueError("A maintained checkout requires a branch name, not HEAD")
    git("check-ref-format", f"refs/heads/{ref}")
    return ref


def inspect_checkout(path, name):
    if not path:
        raise ValueError("Repository directory must not be empty")
    source = Path(inspect_repository(path, "HEAD"))
    root = Path(git("rev-parse", "--show-toplevel", cwd=source)).resolve()
    if root != source:
        raise ValueError("Repository directory must be the checkout root")
    origin = github_name(git("remote", "get-url", "origin", cwd=source))
    if origin.casefold() != name.casefold():
        raise ValueError("Checkout origin does not match the registered GitHub repository")
    return str(source)


class Repository:
    def __init__(self, paths, record):
        self.record = record
        key = hashlib.sha256(record["name"].encode()).hexdigest()[:16]
        self.directory = paths.root / "repos" / key
        self.mirror = self.directory / "mirror.git"
        self.worktrees = self.directory / "worktrees"

    def update_checkout(self, token):
        ref = validate_branch(self.record["ref"])
        directory = inspect_checkout(self.record["directory"], self.record["name"])
        if git("status", "--porcelain", "--untracked-files=all", cwd=directory):
            raise ValueError("Maintained checkout has uncommitted changes; commit or stash them")
        source = f"https://github.com/{github_name(self.record['name'])}.git"
        tracking = f"refs/remotes/origin/{ref}"
        git(
            "fetch",
            "--no-tags",
            "--",
            source,
            f"+refs/heads/{ref}:{tracking}",
            cwd=directory,
            token=token,
        )
        sha = git("rev-parse", "FETCH_HEAD^{commit}", cwd=directory)
        branch = git("for-each-ref", "--format=%(refname)", f"refs/heads/{ref}", cwd=directory)
        if f"refs/heads/{ref}" in branch.splitlines():
            counts = git(
                "rev-list", "--left-right", "--count", f"refs/heads/{ref}...{sha}", cwd=directory
            )
            if all(int(count) for count in counts.split()):
                raise ValueError(f"Maintained branch {ref!r} has diverged; cannot fast-forward")
            git("switch", "--no-guess", "--no-overwrite-ignore", ref, cwd=directory)
            git("merge", "--ff-only", "--no-autostash", "--no-overwrite-ignore", sha, cwd=directory)
        else:
            git("switch", "--no-track", "--no-overwrite-ignore", "-c", ref, sha, cwd=directory)
        return directory

    def prepare(self, scan_id, token=None, *, revision=None):
        self.worktrees.mkdir(parents=True, exist_ok=True)
        if not self.mirror.exists():
            git("init", "--bare", str(self.mirror))
        remote = self.record.get("source_kind", "local") == "github"
        source = self.record["source"]
        if remote and source != f"https://github.com/{github_name(self.record['name'])}.git":
            raise ValueError("Remote source does not match the registered GitHub repository")
        ref = revision or self.record["ref"]
        if revision is not None and not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", revision):
            raise ValueError("Assessment requires a pinned commit SHA")
        if revision is None and self.record.get("directory") is not None:
            source = self.update_checkout(token)
            ref, remote = f"refs/heads/{ref}", False
        git(
            "-C",
            str(self.mirror),
            "fetch",
            "--no-tags",
            "--",
            source,
            ref,
            token=token if remote else None,
        )
        sha = git("-C", str(self.mirror), "rev-parse", "FETCH_HEAD^{commit}")
        if revision is not None and sha != revision:
            raise ValueError("Fetched revision does not match the assessment input")
        worktree = self.worktrees / scan_id
        git("-C", str(self.mirror), "worktree", "add", "--detach", str(worktree), sha)
        return sha, worktree

    def prepare_local(self, scan_id, token=None, *, revision):
        if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", revision):
            raise ValueError("Local snapshot requires a full commit SHA")
        if not re.fullmatch(r"[a-f0-9]{32}(?:-[a-z0-9-]+)?", scan_id):
            raise ValueError("Invalid local snapshot identity")
        sha = git("-C", str(self.mirror), "rev-parse", "--verify", f"{revision}^{{commit}}")
        if sha != revision:
            raise ValueError("Local snapshot revision mismatch")
        self.worktrees.mkdir(parents=True, exist_ok=True)
        worktree = self.worktrees / scan_id
        git("-C", str(self.mirror), "worktree", "add", "--detach", str(worktree), sha)
        return sha, worktree

    def fetch_revision(self, sha, token):
        if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", sha):
            raise ValueError("Remote snapshot requires a full commit SHA")
        self.worktrees.mkdir(parents=True, exist_ok=True)
        if not self.mirror.exists():
            git("init", "--bare", str(self.mirror))
        source = f"https://github.com/{github_name(self.record['name'])}.git"
        git("-C", str(self.mirror), "fetch", "--no-tags", "--", source, sha, token=token)
        if git("-C", str(self.mirror), "rev-parse", "FETCH_HEAD^{commit}") != sha:
            raise ValueError("Fetched snapshot does not match its pinned revision")

    def fetch_pull(self, number, head, base_branch, base_sha, token):
        validate_branch(base_branch)
        self.fetch_revision(base_sha, token)
        source = f"https://github.com/{github_name(self.record['name'])}.git"
        git(
            "-C",
            str(self.mirror),
            "fetch",
            "--no-tags",
            "--",
            source,
            f"refs/pull/{int(number)}/head",
            token=token,
        )
        if git("-C", str(self.mirror), "rev-parse", "FETCH_HEAD^{commit}") != head:
            raise ValueError("PR head changed while fetching")

    def push_repair(self, branch, commit, token, expected=None):
        if not re.fullmatch(r"maxwell/repair/[a-f0-9]{32}", branch):
            raise ValueError("Invalid repair publication branch")
        source = f"https://github.com/{github_name(self.record['name'])}.git"
        remote = git(
            "-C",
            str(self.mirror),
            "ls-remote",
            "--heads",
            source,
            f"refs/heads/{branch}",
            token=token,
        )
        actual = remote.split()[0] if remote else None
        if actual == commit:
            return
        if actual != expected:
            raise ValueError("Remote repair branch changed; refusing to overwrite it")
        lease = f"--force-with-lease=refs/heads/{branch}:{expected or ''}"
        git(
            "-C",
            str(self.mirror),
            "push",
            lease,
            "--",
            source,
            f"{commit}:refs/heads/{branch}",
            token=token,
        )
        remote = git(
            "-C",
            str(self.mirror),
            "ls-remote",
            "--heads",
            source,
            f"refs/heads/{branch}",
            token=token,
        )
        if not remote or remote.split()[0] != commit:
            raise ValueError("Repair branch publication requires reconciliation")

    def merge_candidate(self, identity, head, base):
        _, worktree = self.prepare_local(identity, revision=base)
        try:
            if head == base:
                return base, worktree
            git(
                "-c",
                "user.name=Maxwell",
                "-c",
                "user.email=maxwell@localhost",
                "-c",
                "commit.gpgsign=false",
                "merge",
                "--no-ff",
                "--no-edit",
                "--no-autostash",
                head,
                cwd=worktree,
            )
            return git("rev-parse", "HEAD", cwd=worktree), worktree
        except ValueError as exc:
            conflicts = git("diff", "--name-only", "--diff-filter=U", cwd=worktree)
            self.cleanup(worktree)
            if conflicts:
                raise RepairMergeConflict(conflicts.splitlines()) from exc
            raise

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
    markers = (
        str(Path(artifact) / filename)
        for filename in (
            "result.json",
            "settings.json",
            "format-retry/result.json",
            "format-retry/settings.json",
        )
    )
    if not any(marker in command for marker in markers):
        return
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
