import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

from maxwell.config import Paths


@pytest.fixture
def directory():
    with tempfile.TemporaryDirectory(prefix="mx-", dir="/tmp") as name:
        yield Path(name).resolve()


@pytest.fixture
def source(directory):
    path = directory / "source"
    path.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(path)], check=True)
    (path / "app.py").write_text("def average(values):\n    return sum(values) / len(values)\n")
    subprocess.run(["git", "-C", str(path), "add", "app.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return path


@pytest.fixture
def finding():
    return {
        "title": "Empty input causes division by zero",
        "kind": "bug",
        "priority": "P1",
        "path": "app.py",
        "line": 2,
        "symbol": "average",
        "confidence": 0.95,
        "evidence": "The denominator is len(values), which is zero for an empty list.",
        "expected": "The public function should handle empty input explicitly.",
    }


@pytest.fixture
def executable(directory, finding):
    path = directory / "fake-codex"
    source = """import json, os, sys, time
from pathlib import Path
args = sys.argv
if args[1] == 'app-server':
    for line in sys.stdin:
        message = json.loads(line)
        if message['method'] == 'initialized':
            continue
        result = {} if message['method'] == 'initialize' else {'data':[{'model':'fake-model', 'supportedReasoningEfforts':[{'reasoningEffort':'low'}, {'reasoningEffort':'high'}], 'defaultReasoningEffort':'low'}], 'nextCursor':None}
        print(json.dumps({'id':message['id'], 'result':result}), flush=True)
    sys.exit(0)
output = Path(args[args.index('--output-last-message') + 1])
workspace = Path(args[args.index('--cd') + 1])
mode_path = Path(__file__).parent / 'mode'
mode = mode_path.read_text() if mode_path.exists() else 'normal'
sys.stdin.read()
if mode == 'wait':
    print(json.dumps({'type': 'turn.started'}), flush=True)
    time.sleep(60)
if mode == 'modify':
    (workspace / 'app.py').write_text('changed by worker')
result = {'summary': 'Completed discovery. ' + os.environ.get('CODEX_API_KEY', ''),
          'findings': [] if mode == 'empty' else [FINDING]}
if 'ok' in json.loads(Path(args[args.index('--output-schema') + 1]).read_text())['properties']:
    result = {'ok': True}
elif 'verdict' in json.loads(Path(args[args.index('--output-schema') + 1]).read_text())['properties']:
    result = {'summary': 'Source investigation complete.', 'verdict': 'supported',
              'actionability': 'actionable', 'priority': 'P1',
              'evidence': 'app.py:2 divides by len(values), which is zero for an empty list.'}
    assessment_file = Path(__file__).parent / 'assessment.json'
    if assessment_file.exists():
        result.update(json.loads(assessment_file.read_text()))
output.write_text('not json' if mode == 'malformed' else json.dumps(result))
print(json.dumps({'type': 'diagnostic', 'text': os.environ.get('CODEX_API_KEY', '')}), flush=True)
if mode != 'unknown':
    print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1000,
          'cached_input_tokens': 500, 'output_tokens': 100}}), flush=True)
""".replace("FINDING", repr(finding))
    path.write_text(f"#!{sys.executable}\n" + source)
    path.chmod(0o700)
    return path


@pytest.fixture
def github():
    state = {
        "pulls": {},
        "branches": {},
        "reviews": {},
        "issues": {},
        "comments": {},
        "labels": {},
        "creates": 0,
        "drop_create": False,
        "comment_id": 1000,
        "requests": [],
        "unchanged_reads": 0,
        "tokens": {},
        "protection": {
            "required_status_checks": {"strict": True, "contexts": ["tests"]},
            "enforce_admins": {"enabled": True},
        },
        "statuses": [{"context": "tests", "state": "success"}],
        "check_runs": [],
        "merges": [],
    }
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, code, data):
            payload = json.dumps(data).encode()
            etag = '"' + hashlib.sha256(payload).hexdigest() + '"'
            if self.command == "GET" and code == 200 and self.headers.get("If-None-Match") == etag:
                state["unchanged_reads"] += 1
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(code)
            if self.command == "GET" and code == 200:
                self.send_header("ETag", etag)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def authorized(self):
            parts = urlparse(self.path).path.strip("/").split("/")
            repo = "/".join(parts[1:3])
            token = self.headers.get("Authorization")
            with lock:
                state["requests"].append((self.command, repo, token))
                expected = state["tokens"].get(repo)
            if expected and token != expected:
                self.respond(403, {"message": "Wrong repository credential"})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            parsed = urlparse(self.path)
            parts = parsed.path.strip("/").split("/")
            page = int(parse_qs(parsed.query).get("page", ["1"])[0])
            with lock:
                if parts == ["user"]:
                    data = {"login": "maintainer"}
                elif len(parts) == 3 and parts[0] == "repos":
                    data = {
                        "full_name": "/".join(parts[1:3]),
                        "default_branch": state.get("default_branch", "main"),
                    }
                elif len(parts) == 6 and parts[-1] == "protection":
                    if state["protection"] is None:
                        return self.respond(404, {"message": "Branch not protected"})
                    data = state["protection"]
                elif len(parts) == 6 and parts[-1] == "status":
                    data = {"statuses": state["statuses"]}
                elif len(parts) == 6 and parts[-1] == "check-runs":
                    offset = (page - 1) * 100
                    data = {"check_runs": state["check_runs"][offset : offset + 100]}
                elif len(parts) == 5 and parts[-2] == "commits":
                    data = {"sha": state["branches"][parts[-1]]}
                elif len(parts) == 4 and parts[-1] == "pulls":
                    head = parse_qs(parsed.query).get("head", [""])[0].split(":")[-1]
                    data = (
                        [pull for pull in state["pulls"].values() if pull["head"]["ref"] == head]
                        if page == 1
                        else []
                    )
                elif len(parts) == 5 and parts[-2] == "pulls":
                    data = dict(state["pulls"][int(parts[-1])])
                    required = (
                        (state["protection"] or {}).get("required_status_checks") or {}
                    ).get("contexts", [])
                    passed = {
                        item["context"] for item in state["statuses"] if item["state"] == "success"
                    }
                    if data.get("mergeable_state") == "clean" and not set(required) <= passed:
                        data["mergeable_state"] = "blocked"
                elif len(parts) == 6 and parts[-1] == "reviews":
                    data = state["reviews"].get(int(parts[-2]), []) if page == 1 else []
                elif len(parts) == 4 and parts[-1] == "issues":
                    data = (
                        [
                            issue
                            for issue in state["issues"].values()
                            if issue["_repo"] == "/".join(parts[1:3])
                        ]
                        if page == 1
                        else []
                    )
                elif len(parts) == 4 and parts[-1] == "labels":
                    data = list(state["labels"].values()) if page == 1 else []
                elif len(parts) == 6 and parts[-1] == "comments":
                    data = state["comments"].get(int(parts[-2]), []) if page == 1 else []
                elif len(parts) == 5 and parts[-2] == "issues":
                    data = state["issues"].get(int(parts[-1]))
                else:
                    return self.respond(404, {"message": "not found"})
                self.respond(200, data)

        def do_POST(self):
            if not self.authorized():
                return
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            parts = urlparse(self.path).path.strip("/").split("/")
            with lock:
                if parts == ["graphql"]:
                    if "query MaxwellRepairChecks" in data["query"]:
                        number = data["variables"]["number"]
                        pull = state["pulls"][number]
                        required = (
                            (state["protection"] or {}).get("required_status_checks") or {}
                        ).get("contexts", [])
                        nodes = [
                            {
                                "context": item["context"],
                                "state": item["state"].upper(),
                                "description": item.get("description", ""),
                                "targetUrl": item.get("target_url"),
                                "isRequired": item["context"] in required,
                            }
                            for item in state["statuses"]
                        ] + [
                            {
                                "databaseId": item.get("id"),
                                "name": item["name"],
                                "status": item["status"].upper(),
                                "conclusion": str(item.get("conclusion", "")).upper(),
                                "detailsUrl": item.get("details_url"),
                                "summary": item.get("output", {}).get("summary"),
                                "text": item.get("output", {}).get("text"),
                                "isRequired": item["name"] in required,
                            }
                            for item in state["check_runs"]
                        ]
                        return self.respond(
                            200,
                            {
                                "data": {
                                    "repository": {
                                        "pullRequest": {
                                            "headRefOid": pull["head"]["sha"],
                                            "baseRefOid": pull["base"]["sha"],
                                            "commits": {
                                                "nodes": [
                                                    {
                                                        "commit": {
                                                            "statusCheckRollup": {
                                                                "contexts": {
                                                                    "nodes": nodes,
                                                                    "pageInfo": {
                                                                        "hasNextPage": False,
                                                                        "endCursor": None,
                                                                    },
                                                                }
                                                            }
                                                        }
                                                    }
                                                ]
                                            },
                                        }
                                    }
                                }
                            },
                        )
                    number = int(data["variables"]["id"].split("-")[-1])
                    state["pulls"][number]["draft"] = "convertPullRequestToDraft" in data["query"]
                    self.respond(200, {"data": {"ok": True}})
                elif parts[-1] == "pulls":
                    number = len(state["issues"]) + 1
                    repo = "/".join(parts[1:3])
                    data.update(
                        number=number,
                        node_id=f"pr-{number}",
                        _repo=repo,
                        user={"login": "maintainer"},
                        state="open",
                        merged=False,
                        merge_commit_sha=None,
                        mergeable=True,
                        mergeable_state="clean",
                        html_url=f"https://github.invalid/{repo}/pull/{number}",
                        labels=[],
                        pull_request={},
                        head={
                            "ref": data["head"],
                            "sha": state["branches"][data["head"]],
                            "repo": {"full_name": repo},
                        },
                        base={
                            "ref": data["base"],
                            "sha": state["branches"][data["base"]],
                            "repo": {"full_name": repo},
                        },
                    )
                    state["pulls"][number] = data
                    state["issues"][number] = data
                    if state.get("drop_pull"):
                        state["drop_pull"] = False
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    self.respond(201, data)
                elif parts[-1] == "reviews":
                    data.update(id=state["comment_id"] + 1, user={"login": "maintainer"})
                    state["reviews"].setdefault(int(parts[-2]), []).append(data)
                    self.respond(201, data)
                elif parts[-1] == "labels":
                    existing = data["name"] in state["labels"]
                    state["labels"][data["name"]] = data
                    self.respond(422 if existing else 201, data)
                elif parts[-1] == "issues":
                    number = len(state["issues"]) + 1
                    data.update(
                        _repo="/".join(parts[1:3]),
                        user={"login": "maintainer"},
                        number=number,
                        html_url=f"https://github.invalid/acme/repo/issues/{number}",
                        state="open",
                        labels=[{"name": label} for label in data.get("labels", [])],
                    )
                    state["issues"][number] = data
                    state["creates"] += 1
                    if state["drop_create"]:
                        state["drop_create"] = False
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    self.respond(201, data)
                elif parts[-1] == "comments":
                    state["comment_id"] += 1
                    data.update(id=state["comment_id"], user={"login": "maintainer"})
                    state["comments"].setdefault(int(parts[-2]), []).append(data)
                    self.respond(201, data)
                else:
                    self.respond(404, {})

        def do_PUT(self):
            if not self.authorized():
                return
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            parts = urlparse(self.path).path.strip("/").split("/")
            if parts[-1] != "merge":
                return self.respond(404, {})
            with lock:
                pull = state["pulls"][int(parts[-2])]
                state["merges"].append(data)
                if data["sha"] != pull["head"]["sha"]:
                    return self.respond(409, {"message": "Head changed"})
                if state.get("merge_error"):
                    return self.respond(state["merge_error"], {"message": "Merge blocked"})
                if not state.get("drop_unmerged") and not state.get("report_merge_only"):
                    pull.update(state="closed", merged=True, merge_commit_sha=data["sha"])
                    state["branches"][pull["base"]["ref"]] = data["sha"]
                if state.get("drop_merge") or state.get("drop_unmerged"):
                    state["drop_merge"] = state["drop_unmerged"] = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.respond(200, {"merged": True, "sha": data["sha"]})

        def do_DELETE(self):
            if not self.authorized():
                return
            parts = urlparse(self.path).path.strip("/").split("/")
            if parts[-2] != "comments":
                return self.respond(404, {})
            with lock:
                for comments in state["comments"].values():
                    comments[:] = [
                        comment for comment in comments if comment["id"] != int(parts[-1])
                    ]
            self.respond(200, {})

        def do_PATCH(self):
            if not self.authorized():
                return
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            parts = urlparse(self.path).path.strip("/").split("/")
            with lock:
                if parts[-2] == "comments":
                    record = next(
                        item
                        for comments in state["comments"].values()
                        for item in comments
                        if item["id"] == int(parts[-1])
                    )
                else:
                    record = state["issues"][int(parts[-1])]
                    if "labels" in data:
                        data["labels"] = [{"name": label} for label in data["labels"]]
                record.update(data)
                self.respond(200, record)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def home(directory, executable, github):
    paths = Paths(directory / "home")
    paths.initialize()
    data = {
        "github": {
            "token": "fake-github-secret",
            "api_url": github["url"],
            "sync_seconds": 1,
            "allowed_users": ["maintainer"],
        },
        "provider": {
            "executable": str(executable),
            "model": "fake-model",
            "api_key": "fake-provider-secret",
            "input_usd_per_mtok": 2,
            "cached_input_usd_per_mtok": 0.2,
            "output_usd_per_mtok": 8,
        },
        "limits": {
            "scan_interval_seconds": 3600,
            "scan_timeout_seconds": 5,
            "shutdown_grace_seconds": 0.1,
        },
    }
    paths.config.write_text(yaml.safe_dump(data))
    paths.config.chmod(0o600)
    return paths
