import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        if self.db.execute("PRAGMA user_version").fetchone()[0] > 4:
            self.db.close()
            raise ValueError("State database was created by a newer Maxwell version")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS repositories (
                name TEXT PRIMARY KEY, source TEXT NOT NULL, ref TEXT NOT NULL,
                paused INTEGER NOT NULL DEFAULT 0, next_scan REAL NOT NULL DEFAULT 0,
                last_sha TEXT
            );
            CREATE TABLE IF NOT EXISTS scans (
                id TEXT PRIMARY KEY, repository TEXT NOT NULL REFERENCES repositories(name),
                state TEXT NOT NULL, priority INTEGER NOT NULL, forced INTEGER NOT NULL,
                created REAL NOT NULL, started REAL, finished REAL, sha TEXT, artifact TEXT,
                lease TEXT, pid INTEGER, reserved INTEGER NOT NULL DEFAULT 0,
                cost INTEGER, usage TEXT, summary TEXT, error TEXT, profile TEXT,
                admitted INTEGER NOT NULL DEFAULT 0, allocated INTEGER NOT NULL DEFAULT 0,
                report_sent INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS findings (
                id TEXT PRIMARY KEY, repository TEXT NOT NULL REFERENCES repositories(name),
                scan_id TEXT NOT NULL REFERENCES scans(id), data TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', issue INTEGER, url TEXT,
                allocated INTEGER, error TEXT, checked REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS dashboards (
                repository TEXT PRIMARY KEY, issue INTEGER, url TEXT,
                state TEXT NOT NULL DEFAULT 'pending', error TEXT
            );
            CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, created REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS scans_ready ON scans(state, priority, created);
            CREATE TABLE IF NOT EXISTS repository_stage_routes (
                repository TEXT NOT NULL REFERENCES repositories(name), stage TEXT NOT NULL,
                provider TEXT NOT NULL, model TEXT NOT NULL, PRIMARY KEY(repository,stage)
            );
            CREATE TABLE IF NOT EXISTS finding_observations (
                finding_id TEXT NOT NULL REFERENCES findings(id),
                scan_id TEXT NOT NULL REFERENCES scans(id), data TEXT NOT NULL,
                PRIMARY KEY(finding_id,scan_id)
            );
        """)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(repositories)")}
            for name, default in (("source_kind", "local"), ("github_account", "default")):
                if name not in columns:
                    self.db.execute(
                        f"ALTER TABLE repositories ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'"
                    )
            if "directory" not in columns:
                self.db.execute("ALTER TABLE repositories ADD COLUMN directory TEXT")
            scan_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(scans)")}
            for name, definition in (
                ("stage", "TEXT NOT NULL DEFAULT 'discovery'"),
                ("provider", "TEXT"),
                ("model", "TEXT"),
                ("execution", "TEXT"),
                ("accounting", "TEXT NOT NULL DEFAULT 'usd'"),
                ("executed", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in scan_columns:
                    self.db.execute(f"ALTER TABLE scans ADD COLUMN {name} {definition}")
            if "executed" not in scan_columns:
                self.db.execute("UPDATE scans SET executed=1 WHERE started IS NOT NULL")
            self.db.execute("PRAGMA user_version=4")

    def close(self):
        self.db.close()

    def rows(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args)]

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def event(self, kind, data):
        self.db.execute(
            "INSERT INTO events(created,kind,data) VALUES(?,?,?)",
            (time.time(), kind, json.dumps(data)),
        )

    def register(
        self,
        name,
        source,
        ref,
        source_kind="local",
        github_account="default",
        directory=None,
        route=None,
    ):
        with self.db:
            self.db.execute(
                "INSERT INTO repositories(name,source,ref,source_kind,github_account,directory) "
                "VALUES(?,?,?,?,?,?)",
                (name, source, ref, source_kind, github_account, directory),
            )
            if route is not None:
                self.write_route(name, "discovery", route)
            self.event("repository_registered", {"repository": name})

    def saved_route(self, repository, stage="discovery"):
        return self.one(
            "SELECT provider,model FROM repository_stage_routes WHERE repository=? AND stage=?",
            (repository, stage),
        )

    def write_route(self, repository, stage, route):
        self.db.execute(
            "INSERT INTO repository_stage_routes(repository,stage,provider,model) VALUES(?,?,?,?) "
            "ON CONFLICT(repository,stage) DO UPDATE SET provider=excluded.provider,model=excluded.model",
            (repository, stage, route["provider"], route["model"]),
        )

    def ensure_route(self, repository, config):
        route = self.saved_route(repository)
        if route is None:
            route = config.route()
            with self.db:
                self.write_route(repository, "discovery", route)
        return config.route(current=route)

    def repository(self, name):
        record = self.one("SELECT * FROM repositories WHERE name=?", (name,))
        if record:
            record["stages"] = {
                row["stage"]: {"provider": row["provider"], "model": row["model"]}
                for row in self.rows(
                    "SELECT * FROM repository_stage_routes WHERE repository=? ORDER BY stage",
                    (name,),
                )
            }
        return record

    def change_repository(self, repository, changes, stage="discovery", route=None):
        allowed = {"source", "source_kind", "directory", "ref", "github_account"}
        if (not changes and route is None) or changes.keys() - allowed:
            raise ValueError("Invalid repository settings")
        with self.db:
            assignments = ",".join(f"{key}=?" for key in changes)
            if changes.keys() & {"source", "directory", "ref"}:
                assignments += ",last_sha=NULL"
            cursor = self.db.execute(
                f"UPDATE repositories SET {assignments + ',' if assignments else ''}next_scan=0 WHERE name=?",
                (*changes.values(), repository),
            )
            if not cursor.rowcount:
                raise ValueError("Unknown repository")
            if route is not None:
                self.write_route(repository, stage, route)
            self.event("repository_changed", {"repository": repository, "changes": changes})
            return self.repository(repository)

    def set_account(self, repository, account):
        with self.db:
            cursor = self.db.execute(
                "UPDATE repositories SET github_account=? WHERE name=?", (account, repository)
            )
            if not cursor.rowcount:
                raise ValueError("Unknown repository")
            self.event("repository_account_changed", {"repository": repository, "account": account})

    def enqueue(self, repository, priority=2, forced=True, execution=None):
        snapshot = json.dumps(execution, sort_keys=True) if execution is not None else None
        with self.db:
            if not self.one("SELECT name FROM repositories WHERE name=?", (repository,)):
                raise ValueError("Unknown repository")
            existing = self.one(
                "SELECT id,state FROM scans WHERE repository=? AND execution IS ? "
                "AND state IN ('queued','running')",
                (repository, snapshot),
            )
            if existing:
                if existing["state"] == "queued":
                    self.db.execute(
                        "UPDATE scans SET priority=MIN(priority,?),forced=MAX(forced,?) WHERE id=?",
                        (priority, int(forced), existing["id"]),
                    )
                return existing["id"]
            identifier = uuid.uuid4().hex
            settings = execution["settings"] if execution is not None else None
            accounting = (
                "usd"
                if settings is None
                or all(
                    settings.get(key) is not None
                    for key in ("input_usd_per_mtok", "output_usd_per_mtok")
                )
                else "unpriced"
            )
            self.db.execute(
                "INSERT INTO scans(id,repository,state,priority,forced,created,stage,provider,model,execution,accounting) "
                "VALUES(?,?,'queued',?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    repository,
                    priority,
                    int(forced),
                    time.time(),
                    execution["stage"] if execution else "discovery",
                    execution["provider"] if execution else None,
                    execution["model"] if execution else None,
                    snapshot,
                    accounting,
                ),
            )
            self.event("scan_queued", {"id": identifier, "repository": repository})
            return identifier

    def bind_execution(self, identifier, execution):
        snapshot = json.dumps(execution, sort_keys=True)
        settings = execution["settings"]
        accounting = (
            "usd"
            if all(
                settings.get(key) is not None
                for key in ("input_usd_per_mtok", "output_usd_per_mtok")
            )
            else "unpriced"
        )
        with self.db:
            self.db.execute(
                "UPDATE scans SET execution=?,stage=?,provider=?,model=?,accounting=?,"
                "reserved=CASE WHEN ?='unpriced' THEN 0 ELSE reserved END WHERE id=? AND execution IS NULL",
                (
                    snapshot,
                    execution["stage"],
                    execution["provider"],
                    execution["model"],
                    accounting,
                    accounting,
                    identifier,
                ),
            )
        return self.one("SELECT * FROM scans WHERE id=?", (identifier,))

    def committed_today(self):
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        row = self.one(
            "SELECT COALESCE(SUM(CASE WHEN cost IS NULL THEN reserved ELSE cost END),0) "
            "AS amount FROM scans WHERE accounting='usd' AND (started>=? OR "
            "(started IS NOT NULL AND cost IS NULL))",
            (start.timestamp(),),
        )
        return row["amount"]

    def claim(self, daily_limit, reservation, queue_max, profile=None, daily_scan_limit=20):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            jobs = self.rows(
                """
                SELECT s.* FROM scans s JOIN repositories r ON r.name=s.repository
                WHERE s.state='queued' AND r.paused=0 AND
                  (SELECT COUNT(*) FROM findings f WHERE f.repository=r.name
                   AND f.state NOT IN ('closed','rejected','confirmed')) < ?
                ORDER BY CASE WHEN priority=0 THEN -1000 ELSE
                    priority - MIN(3, (?-created)/86400.0) END, created
            """,
                (queue_max, time.time()),
            )
            start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            unpriced = self.one(
                "SELECT COUNT(*) AS n FROM scans WHERE accounting='unpriced' "
                "AND started>=? AND (executed=1 OR state='running')",
                (start,),
            )["n"]
            committed = self.committed_today()
            job = next(
                (
                    job
                    for job in jobs
                    if (
                        committed + reservation <= daily_limit
                        if job["accounting"] == "usd"
                        else unpriced < daily_scan_limit
                    )
                ),
                None,
            )
            if job is None:
                return None
            reserved = reservation if job["accounting"] == "usd" else 0
            selected_profile = profile
            if job["execution"]:
                execution = json.loads(job["execution"])
                selected_profile = f"{job['provider']}/{execution['settings']['adapter']}/{job['model']}/{job['stage']}"
            lease = uuid.uuid4().hex
            self.db.execute(
                "UPDATE scans SET state='running',started=?,lease=?,reserved=?,profile=? "
                "WHERE id=?",
                (time.time(), lease, reserved, selected_profile, job["id"]),
            )
            self.event("scan_started", {"id": job["id"], "lease": lease})
            return self.one("SELECT * FROM scans WHERE id=?", (job["id"],))

    def finish(self, job, state, summary, usage, cost, findings=(), error=None):
        with self.db:
            current = self.one("SELECT state,lease FROM scans WHERE id=?", (job["id"],))
            if current != {"state": "running", "lease": job["lease"]}:
                raise ValueError("Stale scan result")
            admitted = []
            for finding in findings:
                identity = finding.identity(job["repository"])
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO findings(id,repository,scan_id,data) VALUES(?,?,?,?)",
                    (identity, job["repository"], job["id"], finding.model_dump_json()),
                )
                if cursor.rowcount:
                    admitted.append(identity)
                self.db.execute(
                    "INSERT OR IGNORE INTO finding_observations(finding_id,scan_id,data) VALUES(?,?,?)",
                    (identity, job["id"], finding.model_dump_json()),
                )
            if admitted and cost is not None:
                quotient, remainder = divmod(cost, len(admitted))
                for index, identity in enumerate(admitted):
                    self.db.execute(
                        "UPDATE findings SET allocated=? WHERE id=?",
                        (quotient + (index < remainder), identity),
                    )
            self.db.execute(
                "UPDATE scans SET state=?,finished=?,cost=?,usage=?,summary=?,error=?,"
                "admitted=?,allocated=?,pid=NULL WHERE id=?",
                (
                    state,
                    time.time(),
                    cost,
                    usage.model_dump_json(),
                    summary,
                    error,
                    len(admitted),
                    cost if admitted and cost is not None else 0,
                    job["id"],
                ),
            )
            if state == "completed":
                self.db.execute(
                    "UPDATE repositories SET last_sha=(SELECT sha FROM scans WHERE id=?) WHERE name=?",
                    (job["id"], job["repository"]),
                )
            self.event("scan_finished", {"id": job["id"], "state": state})

    def pause(self, repository, paused):
        with self.db:
            if repository:
                cursor = self.db.execute(
                    "UPDATE repositories SET paused=? WHERE name=?", (int(paused), repository)
                )
                if not cursor.rowcount:
                    raise ValueError("Unknown repository")
            else:
                self.db.execute("UPDATE repositories SET paused=?", (int(paused),))
            self.event("pause_changed", {"repository": repository, "paused": paused})

    def status(self):
        return {
            "repositories": [
                self.repository(row["name"])
                for row in self.rows("SELECT name FROM repositories ORDER BY name")
            ],
            "scans": self.rows(
                "SELECT id,repository,state,priority,created,summary,error,cost,accounting,stage,provider,model "
                "FROM scans ORDER BY created DESC LIMIT 20"
            ),
            "budget_committed_usd": self.committed_today() / 1_000_000,
            "stage_routes": self.rows(
                "SELECT * FROM repository_stage_routes ORDER BY repository,stage"
            ),
            "publication_problems": self.rows(
                "SELECT id,state,error FROM findings WHERE error IS NOT NULL OR state='uncertain'"
            ),
            "dashboard_problems": self.rows(
                "SELECT repository,state,error FROM dashboards WHERE error IS NOT NULL OR state='uncertain'"
            ),
            "hunter_rating": "unrated; requires independently assessed outcomes",
            "entropy": "unavailable; requires a complete measurement baseline",
        }
