import asyncio
import fcntl
import hashlib
import json
import logging
import os
import signal
import time

from maxwell.config import Limits, ProviderSettings
from maxwell.contracts import Usage, estimate_cost, micros
from maxwell.github import GitHub
from maxwell.provider import create_provider, discovery_prompt
from maxwell.reporting import Reporter, hunter_ratings, scan_report
from maxwell.repositories import Repository, stop_orphan
from maxwell.selection import select_candidates
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
    def __init__(self, paths, config, provider_factory=create_provider):
        self.paths, self.config, self.provider_factory = paths, config, provider_factory
        self.store = None
        self.stopping = asyncio.Event()
        self.wake = asyncio.Event()
        self.active = None
        self.config_changed = asyncio.Event()
        self.config_generation = 0
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
            elif command == "reload_config":
                digest = hashlib.sha256(self.paths.config.read_bytes()).hexdigest()
                applied = self.store.one(
                    "SELECT config_digest,valid FROM config_application WHERE id=1"
                )
                if (
                    digest != data.get("config_digest")
                    or not applied
                    or applied["config_digest"] != digest
                    or not applied["valid"]
                ):
                    raise ValueError("Run `maxwell config apply` successfully before reloading")
                config = self.paths.load()
                config.ready()
                if hashlib.sha256(self.paths.config.read_bytes()).hexdigest() != digest:
                    raise ValueError(
                        "Configuration changed while reloading; run `maxwell config apply` again"
                    )
                self.config = config
                self.config_generation += 1
                self.config_changed.set()
                self.wake.set()
                result = {"daemon": "reloaded"}
            elif command in ("pause", "resume"):
                self.store.pause(repo, command == "pause")
                self.wake.set()
                result = {"repository": repo or "all", "paused": command == "pause"}
            elif command == "scan":
                priority = data.get("priority", 2)
                if not isinstance(priority, int) or priority not in range(4):
                    raise ValueError("Priority must be P0–P3")
                repositories = (
                    [self.store.one("SELECT * FROM repositories WHERE name=?", (repo,))]
                    if repo
                    else self.store.rows("SELECT * FROM repositories")
                )
                if any(record is None for record in repositories):
                    raise ValueError("Unknown repository")
                if not data.get("model"):
                    raise ValueError("Manual scans require --model")
                executions = [
                    self.config.execution(
                        self.config.route(
                            data.get("provider"),
                            data["model"],
                            effort=data.get("effort"),
                        ),
                        record,
                    )
                    for record in repositories
                ]
                result = {
                    "scans": [
                        self.store.enqueue(record["name"], priority, execution=execution)
                        for record, execution in zip(repositories, executions, strict=True)
                    ]
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

    async def scan(self, job, on_progress=None):
        def progress(phase):
            if on_progress:
                on_progress(phase)

        settings = self.config.provider
        artifact = self.paths.root / "artifacts" / job["id"]
        artifact.mkdir(parents=True, exist_ok=True)
        provider = None
        worktree = None
        state, summary, error, findings, cost = "failed", "Discovery failed.", None, [], None
        usage = Usage()
        selection = None
        try:
            if not job.get("execution"):
                record = self.store.one(
                    "SELECT * FROM repositories WHERE name=?", (job["repository"],)
                )
                route = self.config.route()
                job = self.store.bind_execution(job["id"], self.config.execution(route, record))
            execution = json.loads(job["execution"])
            if execution["stage"] != "discovery":
                raise ValueError("Only discovery execution is implemented")
            record = execution["repository"]
            settings = ProviderSettings.model_validate(execution["settings"])
            name = self.config.provider_name(execution["provider"])
            settings.api_key = self.config.all_providers()[name].api_key
            scan_config = self.config.model_copy(
                update={
                    "provider": settings,
                    "limits": Limits.model_validate(execution["limits"]),
                }
            )
            repository = Repository(self.paths, record)
            provider = self.provider_factory(self.paths, scan_config)
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
            progress("preparing repository")
            sha, worktree = await asyncio.to_thread(repository.prepare, job["id"], token)
            progress(f"source ready at {sha[:12]}")
            with self.store.db:
                self.store.db.execute("UPDATE scans SET sha=? WHERE id=?", (sha, job["id"]))
            previous = self.store.one(
                "SELECT id FROM scans WHERE repository=? AND execution IS ? AND sha=? "
                "AND state='completed' LIMIT 1",
                (job["repository"], job.get("execution"), sha),
            )
            if previous and not job["forced"]:
                state, summary, cost = "skipped", "Revision already scanned; no model call.", 0
            else:
                progress("loading existing issues from GitHub")
                client = GitHub(access)
                try:
                    known, remote_identities = await Reporter(
                        self.store, client, self.config, self.paths
                    ).discovery_context(job["repository"])
                finally:
                    client.close()
                prompt = discovery_prompt(
                    job["repository"],
                    sha,
                    scan_config.limits.max_findings_per_scan,
                    known,
                    candidate_limit=scan_config.limits.max_candidates_per_scan,
                )
                (artifact / "prompt.txt").write_text(self.config.redact(prompt))
                policy = execution or self.config.model_dump(mode="json")
                (artifact / "policy.json").write_text(
                    self.config.redact(json.dumps(policy, indent=2))
                )
                (artifact / "policy.sha256").write_text(
                    hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
                )

                def started(pid):
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE scans SET pid=?,executed=1 WHERE id=?", (pid, job["id"])
                        )
                    progress("agent running")

                progress("starting agent")
                result = await provider.scan(worktree, artifact, prompt, started)
                if len(result.findings) > scan_config.limits.max_candidates_per_scan:
                    raise ValueError("Provider exceeded limits.max_candidates_per_scan")
                progress(f"validating {len(result.findings)} candidate findings")
                if not await asyncio.to_thread(repository.unchanged, worktree, sha):
                    raise ValueError("Discovery modified its worktree; findings rejected")
                slots = (
                    scan_config.limits.human_queue_max
                    - self.store.one(
                        "SELECT COUNT(*) AS n FROM findings WHERE repository=? AND state NOT IN ('closed','confirmed','rejected')",
                        (job["repository"],),
                    )["n"]
                )
                progress(f"ranking {len(result.findings)} candidates")
                selected, observations, selection = select_candidates(
                    result.findings,
                    job["repository"],
                    worktree,
                    publication_limit=scan_config.limits.max_findings_per_scan,
                    slots=slots,
                    minimum_confidence=scan_config.limits.minimum_confidence,
                    local_ids={
                        row["id"]
                        for row in self.store.rows(
                            "SELECT id FROM findings WHERE repository=?", (job["repository"],)
                        )
                    },
                    remote_ids=remote_identities,
                )
                findings = selected + observations
                progress(f"selected {selection.selected} candidates; skipped {selection.skipped}")
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
            if provider is not None and provider.started:
                usage = provider.usage
                cost = estimate_cost(usage, settings)
            elif cost is None:
                cost = 0
            if job.get("accounting") == "unpriced" and provider is not None and provider.started:
                cost = None
            self.store.finish(
                job,
                state,
                self.config.redact(summary),
                usage,
                cost,
                findings,
                error,
                selection=selection if state == "completed" else None,
            )
            current = self.store.one("SELECT * FROM scans WHERE id=?", (job["id"],))
            (artifact / "summary.json").write_text(json.dumps(current, indent=2))
            (self.paths.root / "reports" / f"{job['id']}.md").write_text(
                self.config.redact(scan_report(current))
            )
            if worktree:
                try:
                    progress("cleaning up worktree")
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
            for old in self.store.rows(
                "SELECT id,repository FROM scans WHERE state='queued' AND execution IS NULL"
            ):
                try:
                    record = self.store.one(
                        "SELECT * FROM repositories WHERE name=?", (old["repository"],)
                    )
                    route = self.config.route()
                    self.store.bind_execution(old["id"], self.config.execution(route, record))
                except ValueError as exc:
                    self.log.warning(
                        "Cannot resolve queued scan %s: %s", old["id"], self.config.redact(str(exc))
                    )
            for repo in self.store.rows(
                "SELECT * FROM repositories WHERE paused=0 AND next_scan<=?", (time.time(),)
            ):
                try:
                    route = self.config.route()
                    self.store.enqueue(
                        repo["name"], forced=False, execution=self.config.execution(route, repo)
                    )
                except ValueError as exc:
                    self.log.warning(
                        "Cannot schedule %s: %s", repo["name"], self.config.redact(str(exc))
                    )
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
                self.config.limits.daily_scan_limit,
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
        generation = None
        try:
            while not self.stopping.is_set():
                if generation != self.config_generation:
                    for client in clients.values():
                        client.close()
                    clients, reporters = {}, {}
                    generation = self.config_generation
                    self.config_changed.clear()
                for record in self.store.rows("SELECT name,github_account FROM repositories"):
                    if generation != self.config_generation:
                        break
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
                waiting = [
                    asyncio.create_task(self.stopping.wait()),
                    asyncio.create_task(self.config_changed.wait()),
                ]
                try:
                    await asyncio.wait(
                        waiting,
                        timeout=self.config.github.sync_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for task in waiting:
                        task.cancel()
                    await asyncio.gather(*waiting, return_exceptions=True)
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
