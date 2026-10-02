import asyncio
import fcntl
import hashlib
import json
import logging
import os
import signal
import time

from maxwell.contracts import Usage, estimate_cost, micros
from maxwell.github import GitHub
from maxwell.provider import CodexProvider, discovery_prompt
from maxwell.reporting import Reporter, hunter_ratings, scan_report
from maxwell.repositories import Repository, stop_orphan
from maxwell.store import Store


async def request(paths, command, **arguments):
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(paths.socket), 3)
    try:
        writer.write((json.dumps({"version": 1, "command": command, **arguments}) + "\n").encode())
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), 5))
        if "error" in response:
            raise ValueError(response["error"])
        return response
    finally:
        writer.close()
        await writer.wait_closed()


class Daemon:
    def __init__(self, paths, config, provider_factory=CodexProvider):
        self.paths, self.config, self.provider_factory = paths, config, provider_factory
        self.store = None
        self.stopping = asyncio.Event()
        self.wake = asyncio.Event()
        self.active = None
        self.log = logging.getLogger("maxwell")

    async def handle(self, reader, writer):
        try:
            data = json.loads(await asyncio.wait_for(reader.readline(), 5))
            if data.get("version") != 1:
                raise ValueError("Unsupported control protocol")
            command, repo = data.get("command"), data.get("repository")
            if command == "status":
                result = {
                    "daemon": "stopping" if self.stopping.is_set() else "running",
                    "pid": os.getpid(),
                    **self.store.status(),
                    "ratings": hunter_ratings(self.store),
                }
            elif command == "stop":
                self.stopping.set()
                self.wake.set()
                result = {"daemon": "stopping"}
            elif command in ("pause", "resume"):
                self.store.pause(repo, command == "pause")
                self.wake.set()
                result = {"repository": repo or "all", "paused": command == "pause"}
            elif command == "scan":
                priority = data.get("priority", 2)
                if not isinstance(priority, int) or priority not in range(4):
                    raise ValueError("Priority must be P0–P3")
                repositories = (
                    [{"name": repo}] if repo else self.store.rows("SELECT name FROM repositories")
                )
                result = {
                    "scans": [self.store.enqueue(row["name"], priority) for row in repositories]
                }
                self.wake.set()
            else:
                raise ValueError("Unknown control command")
        except Exception as exc:
            result = {"error": self.config.redact(str(exc))}
        try:
            writer.write((json.dumps(result) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def recover(self):
        for job in self.store.rows("SELECT * FROM scans WHERE state='running'"):
            stop_orphan(job["pid"], job["artifact"])
            self.store.finish(
                job,
                "interrupted",
                "Daemon stopped before completion; spending may be unknown.",
                Usage(),
                None,
                error="Interrupted; explicit rescan is allowed",
            )
            self.log.warning(
                "Recovered interrupted scan %s; retained budget reservation", job["id"]
            )

    async def scan(self, job):
        record = self.store.one("SELECT * FROM repositories WHERE name=?", (job["repository"],))
        repository = Repository(self.paths, record)
        artifact = self.paths.root / "artifacts" / job["id"]
        artifact.mkdir(parents=True, exist_ok=True)
        provider = self.provider_factory(self.paths, self.config)
        worktree = None
        state, summary, error, findings, cost = "failed", "Discovery failed.", None, [], None
        usage = Usage()
        try:
            with self.store.db:
                self.store.db.execute(
                    "UPDATE scans SET artifact=? WHERE id=?", (str(artifact), job["id"])
                )
            access = self.config.github.for_account(record["github_account"])
            token = access.token.get_secret_value()
            if not token:
                raise ValueError(
                    f"Configure a token for GitHub account {record['github_account']!r}"
                )
            sha, worktree = await asyncio.to_thread(repository.prepare, job["id"], token)
            with self.store.db:
                self.store.db.execute("UPDATE scans SET sha=? WHERE id=?", (sha, job["id"]))
            if sha == record["last_sha"] and not job["forced"]:
                state, summary, cost = "skipped", "Revision already scanned; no model call.", 0
            else:
                known = [
                    json.loads(row["data"])["title"]
                    for row in self.store.rows(
                        "SELECT data FROM findings WHERE repository=? ORDER BY rowid DESC LIMIT 100",
                        (job["repository"],),
                    )
                ]
                prompt = discovery_prompt(
                    job["repository"], sha, self.config.limits.max_findings_per_scan, known
                )
                (artifact / "prompt.txt").write_text(self.config.redact(prompt))
                policy = self.config.model_dump(mode="json")
                (artifact / "policy.json").write_text(json.dumps(policy, indent=2))
                (artifact / "policy.sha256").write_text(
                    hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
                )

                def started(pid):
                    with self.store.db:
                        self.store.db.execute("UPDATE scans SET pid=? WHERE id=?", (pid, job["id"]))

                result = await provider.scan(worktree, artifact, prompt, started)
                if not await asyncio.to_thread(repository.unchanged, worktree, sha):
                    raise ValueError("Discovery modified its worktree; findings rejected")
                slots = (
                    self.config.limits.human_queue_max
                    - self.store.one(
                        "SELECT COUNT(*) AS n FROM findings WHERE repository=? AND state NOT IN ('closed','confirmed','rejected')",
                        (job["repository"],),
                    )["n"]
                )
                seen = set()
                for finding in sorted(
                    result.findings, key=lambda item: (item.priority, -item.confidence)
                ):
                    finding.validate_location(worktree)
                    if finding.confidence < self.config.limits.minimum_confidence:
                        continue
                    identity = finding.identity(job["repository"])
                    if identity in seen:
                        continue
                    seen.add(identity)
                    if self.store.one(
                        "SELECT id FROM findings WHERE id=?", (finding.identity(job["repository"]),)
                    ):
                        continue
                    if len(findings) < min(slots, self.config.limits.max_findings_per_scan):
                        findings.append(finding)
                state, summary = "completed", result.summary
        except asyncio.CancelledError:
            state, summary, error = "cancelled", "Discovery cancelled during shutdown.", "Cancelled"
            findings = []
        except Exception as exc:
            findings = []
            error = self.config.redact(str(exc))[:2000]
            summary = f"Discovery failed: {error}"
            self.log.warning("Scan %s failed: %s", job["id"], error)
        finally:
            if provider.started:
                usage = provider.usage
                cost = estimate_cost(usage, self.config.provider)
            elif cost is None:
                cost = 0
            self.store.finish(job, state, self.config.redact(summary), usage, cost, findings, error)
            current = self.store.one("SELECT * FROM scans WHERE id=?", (job["id"],))
            (artifact / "summary.json").write_text(json.dumps(current, indent=2))
            (self.paths.root / "reports" / f"{job['id']}.md").write_text(
                self.config.redact(scan_report(current))
            )
            if worktree:
                try:
                    await asyncio.to_thread(repository.cleanup, worktree)
                except Exception as exc:
                    self.log.warning(
                        "Worktree cleanup failed for %s: %s",
                        job["id"],
                        self.config.redact(str(exc)),
                    )
            self.log.info("Scan %s: %s, cost_microusd=%s", job["id"], state, cost)

    async def worker(self):
        profile = f"codex/{self.config.provider.model}/discovery-v1"
        while not self.stopping.is_set():
            for repo in self.store.rows(
                "SELECT * FROM repositories WHERE paused=0 AND next_scan<=?", (time.time(),)
            ):
                self.store.enqueue(repo["name"], forced=False)
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE repositories SET next_scan=? WHERE name=?",
                        (time.time() + self.config.limits.scan_interval_seconds, repo["name"]),
                    )
            job = self.store.claim(
                micros(self.config.limits.daily_budget_usd),
                micros(self.config.limits.scan_reservation_usd),
                self.config.limits.human_queue_max,
                profile,
            )
            if job:
                self.active = asyncio.create_task(self.scan(job))
                await self.active
                self.active = None
            else:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), 1)
                except TimeoutError:
                    pass

    async def synchronize(self):
        clients, reporters = {}, {}
        try:
            while not self.stopping.is_set():
                for record in self.store.rows("SELECT name,github_account FROM repositories"):
                    name, account = record["name"], record["github_account"]
                    try:
                        if account not in clients:
                            clients[account] = GitHub(self.config.github.for_account(account))
                        key = (name, account)
                        if key not in reporters:
                            reporters[key] = Reporter(
                                self.store, clients[account], self.config, self.paths
                            )
                        await reporters[key].sync(repository=name)
                    except Exception as exc:
                        with self.store.db:
                            self.store.db.execute(
                                "INSERT OR IGNORE INTO dashboards(repository) VALUES(?)", (name,)
                            )
                            self.store.db.execute(
                                "UPDATE dashboards SET error=? WHERE repository=?",
                                (self.config.redact(str(exc))[:1000], name),
                            )
                try:
                    await asyncio.wait_for(self.stopping.wait(), self.config.github.sync_seconds)
                except TimeoutError:
                    pass
        finally:
            for client in clients.values():
                client.close()

    async def serve(self):
        self.paths.initialize()
        if len(os.fsencode(self.paths.socket)) > 100:
            raise ValueError(
                "Maxwell home path is too long for a Unix socket; choose a shorter --home"
            )
        with self.paths.lock.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("Maxwell is already running") from exc
            self.store = Store(self.paths.db)
            server = None
            tasks = []
            loop = asyncio.get_running_loop()
            try:
                self.recover()
                if self.paths.socket.exists():
                    if (
                        not self.paths.socket.is_socket()
                        or self.paths.socket.stat().st_uid != os.getuid()
                    ):
                        raise ValueError("Refusing to replace unexpected control socket path")
                    self.paths.socket.unlink()
                server = await asyncio.start_unix_server(self.handle, path=self.paths.socket)
                self.paths.socket.chmod(0o600)
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, self.stopping.set)
                tasks = [
                    asyncio.create_task(self.worker()),
                    asyncio.create_task(self.synchronize()),
                ]
                stopped = asyncio.create_task(self.stopping.wait())
                self.log.info("Daemon ready, pid=%s", os.getpid())
                done, _ = await asyncio.wait([*tasks, stopped], return_when=asyncio.FIRST_COMPLETED)
                self.stopping.set()
                self.wake.set()
                for task in done:
                    if task is not stopped and not task.cancelled():
                        task.result()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(tasks[0]), self.config.limits.shutdown_grace_seconds
                    )
                except TimeoutError:
                    tasks[0].cancel()
                stopped.cancel()
                await asyncio.gather(stopped, return_exceptions=True)
            finally:
                self.stopping.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if server:
                    server.close()
                    await server.wait_closed()
                    self.paths.socket.unlink(missing_ok=True)
                self.store.close()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.remove_signal_handler(sig)
                self.log.info("Daemon stopped")
