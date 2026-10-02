import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        if self.db.execute("PRAGMA user_version").fetchone()[0] > 2:
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
        """)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(repositories)")}
            for name, default in (("source_kind", "local"), ("github_account", "default")):
                if name not in columns:
                    self.db.execute(
                        f"ALTER TABLE repositories ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'"
                    )
            self.db.execute("PRAGMA user_version=2")

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

    def register(self, name, source, ref, source_kind="local", github_account="default"):
        with self.db:
            self.db.execute(
                "INSERT INTO repositories(name,source,ref,source_kind,github_account) VALUES(?,?,?,?,?)",
                (name, source, ref, source_kind, github_account),
            )
            self.event("repository_registered", {"repository": name})

    def set_account(self, repository, account):
        with self.db:
            cursor = self.db.execute(
                "UPDATE repositories SET github_account=? WHERE name=?", (account, repository)
            )
            if not cursor.rowcount:
                raise ValueError("Unknown repository")
            self.event("repository_account_changed", {"repository": repository, "account": account})

    def enqueue(self, repository, priority=2, forced=True):
        with self.db:
            if not self.one("SELECT name FROM repositories WHERE name=?", (repository,)):
                raise ValueError("Unknown repository")
            existing = self.one(
                "SELECT id,state FROM scans WHERE repository=? AND state IN ('queued','running')",
                (repository,),
            )
            if existing:
                if existing["state"] == "queued":
                    self.db.execute(
                        "UPDATE scans SET priority=MIN(priority,?),forced=MAX(forced,?) WHERE id=?",
                        (priority, int(forced), existing["id"]),
                    )
                return existing["id"]
            identifier = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO scans(id,repository,state,priority,forced,created) "
                "VALUES(?,?,'queued',?,?,?)",
                (identifier, repository, priority, int(forced), time.time()),
            )
            self.event("scan_queued", {"id": identifier, "repository": repository})
            return identifier

    def committed_today(self):
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        row = self.one(
            "SELECT COALESCE(SUM(CASE WHEN cost IS NULL THEN reserved ELSE cost END),0) "
            "AS amount FROM scans WHERE started>=? OR "
            "(started IS NOT NULL AND cost IS NULL)",
            (start.timestamp(),),
        )
        return row["amount"]

    def claim(self, daily_limit, reservation, queue_max, profile):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.committed_today() + reservation > daily_limit:
                return None
            job = self.one(
                """
                SELECT s.* FROM scans s JOIN repositories r ON r.name=s.repository
                WHERE s.state='queued' AND r.paused=0 AND
                  (SELECT COUNT(*) FROM findings f WHERE f.repository=r.name
                   AND f.state NOT IN ('closed','rejected','confirmed')) < ?
                ORDER BY CASE WHEN priority=0 THEN -1000 ELSE
                    priority - MIN(3, (?-created)/86400.0) END, created LIMIT 1
            """,
                (queue_max, time.time()),
            )
            if not job:
                return None
            lease = uuid.uuid4().hex
            self.db.execute(
                "UPDATE scans SET state='running',started=?,lease=?,reserved=?,profile=? "
                "WHERE id=?",
                (time.time(), lease, reservation, profile, job["id"]),
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
            "repositories": self.rows("SELECT * FROM repositories ORDER BY name"),
            "scans": self.rows(
                "SELECT id,repository,state,priority,created,summary,error,cost "
                "FROM scans ORDER BY created DESC LIMIT 20"
            ),
            "budget_committed_usd": self.committed_today() / 1_000_000,
            "publication_problems": self.rows(
                "SELECT id,state,error FROM findings WHERE error IS NOT NULL OR state='uncertain'"
            ),
            "dashboard_problems": self.rows(
                "SELECT repository,state,error FROM dashboards WHERE error IS NOT NULL OR state='uncertain'"
            ),
            "hunter_rating": "unrated; requires independently assessed outcomes",
            "entropy": "unavailable; requires a complete measurement baseline",
        }
