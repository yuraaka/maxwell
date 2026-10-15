import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime

from maxwell.records import (
    RECORD_ADAPTER,
    AssessmentRecord,
    IntegrationRecord,
    RepairPullRequestRecord,
    RepairRecord,
    StageExecution,
    ValidationRecord,
    record_digest,
)

SCHEMA_VERSION = 13


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        if self.db.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
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
            CREATE TABLE IF NOT EXISTS histories (
                repository TEXT PRIMARY KEY, issue INTEGER, url TEXT,
                state TEXT NOT NULL DEFAULT 'pending', error TEXT
            );
            CREATE TABLE IF NOT EXISTS repair_publications (
                repository TEXT NOT NULL, repair_id TEXT NOT NULL, finding_issue INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', number INTEGER, data TEXT, error TEXT,
                PRIMARY KEY(repository,repair_id)
            );
            CREATE TABLE IF NOT EXISTS pr_actions (
                repository TEXT NOT NULL, id TEXT NOT NULL, action TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', error TEXT,
                PRIMARY KEY(repository,id,action)
            );
            CREATE TABLE IF NOT EXISTS pipeline_attempts (
                repository TEXT NOT NULL REFERENCES repositories(name),
                stage TEXT NOT NULL, subject TEXT NOT NULL, fingerprint TEXT NOT NULL,
                attempted REAL NOT NULL, state TEXT NOT NULL, error TEXT,
                PRIMARY KEY(repository,stage,subject,fingerprint)
            );
            CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, created REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS scans_ready ON scans(state, priority, created);
            CREATE TABLE IF NOT EXISTS finding_observations (
                finding_id TEXT NOT NULL REFERENCES findings(id),
                scan_id TEXT NOT NULL REFERENCES scans(id), data TEXT NOT NULL,
                PRIMARY KEY(finding_id,scan_id)
            );
            CREATE TABLE IF NOT EXISTS model_catalogs (
                provider TEXT PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS config_application (
                id INTEGER PRIMARY KEY CHECK(id=1), config_digest TEXT NOT NULL,
                checked_at REAL NOT NULL, valid INTEGER NOT NULL, checks TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS github_records (
                repository TEXT NOT NULL REFERENCES repositories(name),
                type TEXT NOT NULL, id TEXT NOT NULL, issue INTEGER NOT NULL,
                comment INTEGER NOT NULL DEFAULT 0, author TEXT NOT NULL,
                data TEXT NOT NULL, digest TEXT NOT NULL, source_digest TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'current', observed REAL NOT NULL,
                PRIMARY KEY(repository,type,id), UNIQUE(repository,issue,comment)
            );
            CREATE TABLE IF NOT EXISTS github_record_history (
                repository TEXT NOT NULL REFERENCES repositories(name),
                issue INTEGER NOT NULL, comment INTEGER NOT NULL,
                source_digest TEXT NOT NULL, author TEXT NOT NULL, data TEXT NOT NULL,
                observed REAL NOT NULL, PRIMARY KEY(repository,issue,comment,source_digest)
            );
            CREATE TABLE IF NOT EXISTS record_publications (
                repository TEXT NOT NULL REFERENCES repositories(name),
                type TEXT NOT NULL, id TEXT NOT NULL, issue INTEGER NOT NULL,
                data TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', error TEXT,
                PRIMARY KEY(repository,type,id)
            );
            CREATE TABLE IF NOT EXISTS stage_executions (
                id TEXT PRIMARY KEY, repository TEXT NOT NULL REFERENCES repositories(name),
                state TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS measurements (
                id TEXT PRIMARY KEY, repository TEXT NOT NULL REFERENCES repositories(name),
                state TEXT NOT NULL, created REAL NOT NULL, finished REAL, sha TEXT,
                artifact TEXT, data TEXT, error TEXT
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
                ("selection", "TEXT"),
            ):
                if name not in scan_columns:
                    self.db.execute(f"ALTER TABLE scans ADD COLUMN {name} {definition}")
            if "executed" not in scan_columns:
                self.db.execute("UPDATE scans SET executed=1 WHERE started IS NOT NULL")
            self.db.execute("DROP TABLE IF EXISTS repository_stage_routes")
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

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

    def project_record(self, record, issue, comment, author, source_digest):
        record = RECORD_ADAPTER.validate_python(record)
        if (
            isinstance(record, AssessmentRecord)
            and record.role == "human"
            and record.actor != author
        ):
            raise ValueError("Human assessment actor does not match GitHub author")
        data, digest = record.model_dump_json(), record_digest(record)
        with self.db:
            existing = self.one(
                "SELECT * FROM github_records WHERE repository=? AND type=? AND id=?",
                (record.repository, record.type, record.id),
            )
            source = self.one(
                "SELECT * FROM github_records WHERE repository=? AND issue=? AND comment=?",
                (record.repository, issue, comment),
            )
            if source and (source["id"], source["type"]) != (record.id, record.type):
                raise ValueError("A GitHub source cannot change record identity")
            if existing and (existing["issue"], existing["comment"]) != (issue, comment):
                raise ValueError("Record identity already belongs to another GitHub source")
            if isinstance(record, RepairPullRequestRecord):
                repair_row = self.one(
                    "SELECT data FROM github_records WHERE repository=? AND issue=? AND type='repair' AND id=? AND status='current'",
                    (record.repository, issue, record.repair_id),
                )
                if not repair_row or issue != record.finding_issue:
                    raise ValueError("PR association requires its repair on the finding issue")
                if record.previous_id:
                    prior_row = self.one(
                        "SELECT data FROM github_records WHERE repository=? AND type='pull_request' AND id=? AND status='current'",
                        (record.repository, record.previous_id),
                    )
                    if not prior_row:
                        raise ValueError("PR revision predecessor is missing")
                    prior = RECORD_ADAPTER.validate_json(prior_row["data"])
                    if (
                        prior.number,
                        prior.finding_id,
                        prior.finding_issue,
                        prior.branch,
                        prior.base_branch,
                    ) != (
                        record.number,
                        record.finding_id,
                        record.finding_issue,
                        record.branch,
                        record.base_branch,
                    ):
                        raise ValueError("PR revision predecessor belongs to a different PR")
                repair = RECORD_ADAPTER.validate_json(repair_row["data"])
                if (
                    repair.finding_id != record.finding_id
                    or repair.sha != record.sha
                    or repair.branch != record.branch
                    or (record.provenance == "fixer" and repair.commit != record.head_sha)
                ):
                    raise ValueError("PR association does not match its repair")
            if isinstance(record, ValidationRecord):
                subject_issue = issue
                association = None
                if record.pr_id:
                    link = self.one(
                        "SELECT data FROM github_records WHERE repository=? AND type='pull_request' AND id=? AND status='current'",
                        (record.repository, record.pr_id),
                    )
                    if not link:
                        raise ValueError("Validation requires its PR association")
                    association = RECORD_ADAPTER.validate_json(link["data"])
                    subject_issue = association.finding_issue
                    if not isinstance(record, IntegrationRecord) and record.sha != record.pr_head:
                        raise ValueError("PR validation must identify its exact head revision")
                    if (
                        association.number != issue
                        or association.repair_id != record.repair_id
                        or association.finding_id != record.finding_id
                        or association.head_sha != record.pr_head
                        or association.base_sha != record.pr_base
                    ):
                        raise ValueError("Validation belongs to a different PR revision")
                repair_row = self.one(
                    "SELECT data FROM github_records WHERE repository=? AND issue=? AND type='repair' AND id=? AND status='current'",
                    (record.repository, subject_issue, record.repair_id),
                )
                if not repair_row:
                    raise ValueError("Validation requires its current repair record")
                repair = RECORD_ADAPTER.validate_json(repair_row["data"])
                if (
                    repair.finding_id != record.finding_id
                    or repair.sha != record.base_sha
                    or (not association and repair.commit != record.sha)
                    or repair.outcome != "prepared"
                    or set(repair.references) != set(record.references)
                    or (repair.agent.adapter, repair.agent.model)
                    == (record.agent.adapter, record.agent.model)
                ):
                    raise ValueError(
                        "Validation provenance or independent agent does not match repair"
                    )
                finding_row = self.one(
                    "SELECT data FROM github_records WHERE repository=? AND issue=? AND type='finding' AND status='current'",
                    (record.repository, subject_issue),
                )
                if not finding_row:
                    raise ValueError("Validation requires its current finding")
                subject = RECORD_ADAPTER.validate_json(finding_row["data"])
                if subject.id != record.finding_id or subject.sha != record.base_sha:
                    raise ValueError("Validation targets a different finding or revision")
                if (
                    record.outcome == "accepted"
                    and subject.finding.kind == "bug"
                    and (
                        not record.replay
                        or record.replay.state != "failed"
                        or record.replay.returncode != 1
                        or not record.failures
                        or any(item.get("phase") != "call" for item in record.failures)
                        or record.result.reproduction != "reproduced"
                    )
                ):
                    raise ValueError(
                        "Accepted bug validation requires original-defect reproduction"
                    )
                for reference in record.supersedes:
                    previous = self.one(
                        "SELECT issue,data FROM github_records WHERE repository=? AND type=? AND id=?",
                        (record.repository, record.type, reference),
                    )
                    if not previous:
                        raise ValueError("Superseded validation is missing")
                    prior = RECORD_ADAPTER.validate_json(previous["data"])
                    if (
                        previous["issue"] != issue
                        or prior.repair_id != record.repair_id
                        or prior.sha != record.sha
                        or prior.pr_id != record.pr_id
                        or prior.pr_head != record.pr_head
                        or prior.pr_base != record.pr_base
                    ):
                        raise ValueError("Validation supersedes a different repair")
            if isinstance(record, RepairRecord):
                finding = self.one(
                    "SELECT data FROM github_records WHERE repository=? AND issue=? "
                    "AND type='finding' AND status='current'",
                    (record.repository, issue),
                )
                if not finding:
                    raise ValueError("Repair requires its current finding record")
                if record.previous_id:
                    prior_row = self.one(
                        "SELECT data FROM github_records WHERE repository=? AND issue=? AND type='repair' AND id=? AND status='current'",
                        (record.repository, issue, record.previous_id),
                    )
                    if not prior_row:
                        raise ValueError("Repair amendment predecessor is missing")
                    prior = RECORD_ADAPTER.validate_json(prior_row["data"])
                    if (
                        prior.branch != record.branch
                        or (prior.lineage_id or prior.execution_id) != record.lineage_id
                        or prior.finding_id != record.finding_id
                    ):
                        raise ValueError("Repair amendment belongs to a different lineage")
                subject = RECORD_ADAPTER.validate_json(finding["data"])
                if subject.id != record.finding_id or subject.sha != record.sha:
                    raise ValueError("Repair targets a different finding or revision")
            if isinstance(record, (AssessmentRecord, RepairRecord, ValidationRecord)):
                for reference in [
                    *record.references,
                    *(
                        [record.supersedes]
                        if isinstance(record, AssessmentRecord) and record.supersedes
                        else []
                    ),
                ]:
                    target = self.one(
                        "SELECT data FROM github_records WHERE repository=? AND type='assessment' AND id=?",
                        (record.repository, reference),
                    )
                    if not target and isinstance(record, ValidationRecord):
                        raise ValueError("Validation assessment reference is missing")
                    if target:
                        assessment = RECORD_ADAPTER.validate_json(target["data"])
                        if assessment.finding_id != record.finding_id or assessment.sha != (
                            record.base_sha if isinstance(record, ValidationRecord) else record.sha
                        ):
                            raise ValueError(
                                "Assessment references a different finding or revision"
                            )
            self.db.execute(
                "INSERT OR IGNORE INTO github_record_history "
                "(repository,issue,comment,source_digest,author,data,observed) VALUES(?,?,?,?,?,?,?)",
                (record.repository, issue, comment, source_digest, author, data, time.time()),
            )
            status = (
                "changed"
                if existing and (existing["digest"] != digest or existing["author"] != author)
                else "current"
            )
            self.db.execute(
                "INSERT INTO github_records "
                "(repository,type,id,issue,comment,author,data,digest,source_digest,status,observed) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(repository,type,id) DO UPDATE SET "
                "source_digest=excluded.source_digest,status=excluded.status,observed=excluded.observed",
                (
                    record.repository,
                    record.type,
                    record.id,
                    issue,
                    comment,
                    author,
                    data,
                    digest,
                    source_digest,
                    status,
                    time.time(),
                ),
            )
        return status

    def reconcile_records(self, repository, issue, seen):
        with self.db:
            for row in self.rows(
                "SELECT comment FROM github_records WHERE repository=? AND issue=?",
                (repository, issue),
            ):
                if row["comment"] not in seen:
                    self.db.execute(
                        "UPDATE github_records SET status='missing' "
                        "WHERE repository=? AND issue=? AND comment=?",
                        (repository, issue, row["comment"]),
                    )

    def queue_record(self, record, issue):
        with self.db:
            self._queue_record(record, issue)

    def _queue_record(self, record, issue):
        record = RECORD_ADAPTER.validate_python(record)
        if record.type == "finding":
            raise ValueError("Findings are published as issues, not comments")
        if isinstance(
            record, (AssessmentRecord, RepairRecord, ValidationRecord, RepairPullRequestRecord)
        ):
            destination = issue
            if isinstance(record, ValidationRecord) and record.pr_id:
                link = self.one(
                    "SELECT data FROM github_records WHERE repository=? AND type='pull_request' AND id=? AND status='current'",
                    (record.repository, record.pr_id),
                )
                if not link or RECORD_ADAPTER.validate_json(link["data"]).number != issue:
                    raise ValueError("Validation must be published on its associated PR")
                destination = RECORD_ADAPTER.validate_json(link["data"]).finding_issue
            finding = self.one(
                "SELECT id FROM findings WHERE repository=? AND issue=? "
                "UNION SELECT id FROM github_records WHERE repository=? AND issue=? AND type='finding'",
                (record.repository, destination, record.repository, destination),
            )
            if not finding or finding["id"] != record.finding_id:
                raise ValueError("Assessment or repair must be published on its finding issue")
        data = record.model_dump_json()
        existing = self.one(
            "SELECT issue,data FROM record_publications WHERE repository=? AND type=? AND id=?",
            (record.repository, record.type, record.id),
        )
        if existing and (
            existing["issue"] != issue
            or record_digest(RECORD_ADAPTER.validate_json(existing["data"]))
            != record_digest(record)
        ):
            raise ValueError("Published records are immutable; use a new superseding record")
        self.db.execute(
            "INSERT OR IGNORE INTO record_publications(repository,type,id,issue,data) "
            "VALUES(?,?,?,?,?)",
            (record.repository, record.type, record.id, issue, data),
        )

    def save_stage_execution(self, execution):
        execution = StageExecution.model_validate(execution)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.one("SELECT data FROM stage_executions WHERE id=?", (execution.id,))
            if existing:
                previous = StageExecution.model_validate_json(existing["data"])
                for field in (
                    "repository",
                    "provider",
                    "agent",
                    "sha",
                    "finding_id",
                    "created",
                    "input_record_ids",
                ):
                    if getattr(previous, field) != getattr(execution, field):
                        raise ValueError("Execution identity and inputs cannot change")
                if previous.state not in ("queued", "running") and previous != execution:
                    raise ValueError("Finished executions cannot change")
                if previous.state == "running" and (
                    execution.state == "queued"
                    or execution.lease != previous.lease
                    or execution.started != previous.started
                ):
                    raise ValueError("Stale stage execution")
            self.db.execute(
                "INSERT INTO stage_executions(id,repository,state,data) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET state=excluded.state,data=excluded.data",
                (execution.id, execution.repository, execution.state, execution.model_dump_json()),
            )

    def apply_models(self, report, digest, *, reused_providers=()):
        checked_at = time.time()
        with self.db:
            self.db.execute("DELETE FROM model_catalogs")
            for catalog in report["providers"]:
                if catalog["provider"] not in reused_providers:
                    catalog["checked_at"] = checked_at
                stored_catalog = {
                    **catalog,
                    "models": [
                        model for model in catalog["models"] if model["access"] != "unavailable"
                    ],
                }
                self.db.execute(
                    "INSERT INTO model_catalogs(provider,data) VALUES(?,?)",
                    (catalog["provider"], json.dumps(stored_catalog)),
                )
            self.db.execute(
                "INSERT INTO config_application(id,config_digest,checked_at,valid,checks) VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET config_digest=excluded.config_digest,checked_at=excluded.checked_at,valid=excluded.valid,checks=excluded.checks",
                (digest, checked_at, report["valid"], json.dumps(report["checks"])),
            )
            self.event(
                "configuration_applied", {"valid": report["valid"], "checked_at": checked_at}
            )
        return checked_at

    @staticmethod
    def cached_models(path, provider=None):
        if not path.exists():
            raise ValueError("Run `maxwell config apply` to discover and check models first")
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            connection.execute("BEGIN")
            if not connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='config_application'"
            ).fetchone():
                raise ValueError("Run `maxwell config apply` to discover and check models first")
            application = connection.execute(
                "SELECT checked_at,valid,config_digest FROM config_application WHERE id=1"
            ).fetchone()
            if not application:
                raise ValueError("Run `maxwell config apply` to discover and check models first")
            rows = connection.execute(
                "SELECT data FROM model_catalogs"
                + (" WHERE provider=?" if provider else "")
                + " ORDER BY provider",
                (provider,) if provider else (),
            ).fetchall()
            if provider and not rows:
                raise ValueError(
                    f"No stored models for provider {provider!r}; run `maxwell config apply`"
                )
            return {
                "providers": [json.loads(row[0]) for row in rows],
                "checked_at": application[0],
                "valid": bool(application[1]),
                "config_digest": application[2],
            }
        finally:
            connection.close()

    def register(
        self,
        name,
        source,
        ref,
        source_kind="local",
        github_account="default",
        directory=None,
    ):
        with self.db:
            self.db.execute(
                "INSERT INTO repositories(name,source,ref,source_kind,github_account,directory) "
                "VALUES(?,?,?,?,?,?)",
                (name, source, ref, source_kind, github_account, directory),
            )
            self.event("repository_registered", {"repository": name})

    def repository(self, name):
        return self.one("SELECT * FROM repositories WHERE name=?", (name,))

    def change_repository(self, repository, changes):
        allowed = {"source", "source_kind", "directory", "ref", "github_account"}
        if not changes or changes.keys() - allowed:
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
            self.event("repository_changed", {"repository": repository, "changes": changes})
            return self.repository(repository)

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
        stages = self.one(
            "SELECT COALESCE(SUM(COALESCE(json_extract(data,'$.cost_micros'),"
            "json_extract(data,'$.reserved_micros'))),0) AS amount FROM stage_executions "
            "WHERE json_extract(data,'$.accounting')='usd' AND "
            "(json_extract(data,'$.started')>=? OR (json_extract(data,'$.started') IS NOT NULL "
            "AND json_extract(data,'$.cost_micros') IS NULL))",
            (start.timestamp(),),
        )
        return row["amount"] + stages["amount"]

    def unpriced_started_today(self):
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        scans = self.one(
            "SELECT COUNT(*) AS n FROM scans WHERE accounting='unpriced' "
            "AND started>=? AND (executed=1 OR state='running')",
            (start,),
        )["n"]
        stages = self.one(
            "SELECT COUNT(*) AS n FROM stage_executions "
            "WHERE json_extract(data,'$.accounting')='unpriced' "
            "AND json_extract(data,'$.started')>=?",
            (start,),
        )["n"]
        return scans + stages

    def admission_blockers(self, identifier, daily_limit, reservation, queue_max, daily_scan_limit):
        job = self.one("SELECT repository,accounting,stage FROM scans WHERE id=?", (identifier,))
        return self.execution_blockers(
            job["repository"],
            job["stage"],
            job["accounting"],
            daily_limit,
            reservation,
            queue_max,
            daily_scan_limit,
        )

    def execution_blockers(
        self, repository, stage, accounting, daily_limit, reservation, queue_max, daily_scan_limit
    ):
        pending = self.one(
            "SELECT COUNT(*) AS n FROM findings WHERE repository=? "
            "AND state NOT IN ('closed','confirmed','rejected')",
            (repository,),
        )["n"]
        reasons = []
        if stage == "discovery" and pending >= queue_max:
            reasons.append(
                f"Human review queue is full: {pending}/{queue_max} unresolved findings. "
                "Close reviewed finding issues on GitHub, or increase limits.human_queue_max"
            )
        if accounting == "usd":
            if self.committed_today() + reservation > daily_limit:
                reasons.append(
                    "Daily budget cannot cover another scan reservation. "
                    "Check limits.daily_budget_usd and limits.scan_reservation_usd"
                )
        else:
            started = self.unpriced_started_today()
            if started >= daily_scan_limit:
                reasons.append(
                    f"Daily scan limit reached: {started}/{daily_scan_limit} executions today (UTC). "
                    "Wait until the next UTC day or increase limits.daily_scan_limit"
                )
        return reasons

    def claim(
        self,
        daily_limit,
        reservation,
        queue_max,
        profile=None,
        daily_scan_limit=20,
        *,
        scan_id=None,
        manual=False,
    ):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            jobs = self.rows(
                """
                SELECT s.* FROM scans s JOIN repositories r ON r.name=s.repository
                WHERE s.state='queued' AND (? OR r.paused=0) AND
                  (? IS NULL OR s.id=?) AND
                  (s.stage!='discovery' OR (SELECT COUNT(*) FROM findings f WHERE f.repository=r.name
                   AND f.state NOT IN ('closed','rejected','confirmed')) < ?)
                ORDER BY CASE WHEN priority=0 THEN -1000 ELSE
                    priority - MIN(3, (?-created)/86400.0) END, created
            """,
                (manual, scan_id, scan_id, queue_max, time.time()),
            )
            unpriced = self.unpriced_started_today()
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

    def fail_queued(self, identifier, error):
        with self.db:
            self.db.execute(
                "UPDATE scans SET state='failed',finished=?,cost=0,summary=?,error=? "
                "WHERE id=? AND state='queued'",
                (time.time(), error, error, identifier),
            )
            self.event("scan_blocked", {"id": identifier, "error": error})

    def finish(
        self,
        job,
        state,
        summary,
        usage,
        cost,
        findings=(),
        error=None,
        *,
        selection=None,
        publications=(),
    ):
        with self.db:
            current = self.one("SELECT state,lease FROM scans WHERE id=?", (job["id"],))
            if current != {"state": "running", "lease": job["lease"]}:
                raise ValueError("Stale scan result")
            for record, issue in publications:
                record = record.model_copy(update={"spending": usage.spending})
                self._queue_record(record, issue)
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
                "admitted=?,allocated=?,selection=?,pid=NULL WHERE id=?",
                (
                    state,
                    time.time(),
                    cost,
                    usage.model_dump_json(),
                    summary,
                    error,
                    len(admitted),
                    cost if admitted and cost is not None else 0,
                    selection.model_dump_json() if selection is not None else None,
                    job["id"],
                ),
            )
            if state == "completed" and job.get("stage", "discovery") == "discovery":
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
                "SELECT id,repository,state,priority,created,summary,error,cost,accounting,stage,provider,model,json_extract(execution,'$.settings.effort') AS effort "
                "FROM scans ORDER BY created DESC LIMIT 20"
            ),
            "budget_committed_usd": self.committed_today() / 1_000_000,
            "publication_problems": self.rows(
                "SELECT id,state,error FROM findings WHERE error IS NOT NULL OR state='uncertain'"
            ),
            "dashboard_problems": self.rows(
                "SELECT repository,state,error FROM dashboards WHERE error IS NOT NULL OR state='uncertain'"
            ),
            "history_problems": self.rows(
                "SELECT repository,state,error FROM histories WHERE error IS NOT NULL OR state='uncertain'"
            ),
            "hunter_rating": "unrated; requires independently assessed outcomes",
            "entropy": "unavailable; requires a complete measurement baseline",
        }
