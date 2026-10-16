import hashlib
import json
import logging
import math
from collections import OrderedDict
from datetime import UTC, datetime
from threading import Event, Lock
from time import monotonic, sleep, time

import httpx

from maxwell.contracts import FACTORS


class PublicationUncertain(Exception):
    pass


def rate_limit_message(response):
    if response.status_code not in (403, 429):
        return None
    if response.headers.get("x-ratelimit-remaining") == "0":
        message = "GitHub API rate limit exceeded; remaining requests: 0"
        try:
            reset = datetime.fromtimestamp(int(response.headers["x-ratelimit-reset"]), UTC)
            return f"{message}; resets at {reset:%Y-%m-%d %H:%M:%S UTC}"
        except (KeyError, ValueError, OverflowError, OSError):
            return message
    if response.headers.get("retry-after"):
        return f"GitHub rate limit exceeded; Retry-After: {response.headers['retry-after']} seconds"
    return None


def rate_limit_delay(response, attempt):
    if response.status_code not in (403, 429):
        return None
    primary = response.headers.get("x-ratelimit-remaining") == "0"
    try:
        message = str(response.json().get("message", "")).lower()
    except (ValueError, AttributeError):
        message = ""
    if not (
        primary
        or response.status_code == 429
        or response.headers.get("retry-after")
        or "api rate limit exceeded" in message
        or "secondary rate limit" in message
        or "abuse detection" in message
    ):
        return None
    delays = []
    try:
        retry = float(response.headers["retry-after"])
        if math.isfinite(retry) and retry >= 0:
            delays.append(max(1, retry))
    except (KeyError, ValueError):
        pass
    if primary:
        try:
            reset = int(response.headers["x-ratelimit-reset"]) - time() + 1
            if reset > 0:
                delays.append(reset)
        except (KeyError, ValueError):
            pass
    return max(delays) if delays else min(900, 60 * 2 ** min(attempt, 4))


class GitHub:
    _responses = OrderedDict()
    _response_lock = Lock()
    _max_responses = 512
    _max_response_bytes = 16 * 1024 * 1024

    def __init__(self, settings, *, on_status=None, account="default"):
        if not settings.token.get_secret_value():
            raise ValueError("Configure a token for the repository's GitHub account")
        self.allowed_users = settings.allowed_users
        self.on_status = on_status
        self.account = account
        self._closed = Event()
        self.client = httpx.Client(
            base_url=settings.api_url.rstrip("/") + "/",
            timeout=15,
            headers={
                "Authorization": f"Bearer {settings.token.get_secret_value()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def close(self):
        self._closed.set()
        self.client.close()

    def wait_for_quota(self, response, delay):
        deadline = monotonic() + delay
        retry_at = datetime.fromtimestamp(time() + delay, UTC)
        remaining = response.headers.get("x-ratelimit-remaining", "unknown")
        remaining = remaining if remaining.isdigit() else "unknown"
        kind = "primary" if remaining == "0" else "secondary"
        message = (
            f"waiting for GitHub {kind} quota (HTTP {response.status_code}, remaining {remaining}); "
            f"account {self.account}; retry at {retry_at:%Y-%m-%d %H:%M:%S UTC}"
        )
        logging.getLogger(__name__).warning(message)
        while (left := deadline - monotonic()) > 0:
            if self.on_status:
                self.on_status(f"{message}; {math.ceil(left)}s left; job retained")
            if self._closed.wait(min(30, left)):
                raise ValueError("GitHub request stopped while waiting for quota")
        message = "GitHub quota wait finished; retrying the same request"
        logging.getLogger(__name__).info(message)
        if self.on_status:
            self.on_status(message)

    def request(self, method, path, **kwargs):
        method = method.upper()
        request = self.client.build_request(
            method, path, params=kwargs.get("params"), headers=kwargs.get("headers")
        )
        account = (
            str(self.client.base_url),
            hashlib.sha256(request.headers.get("Authorization", "").encode()).hexdigest(),
        )
        key = (
            *account,
            str(request.url),
            request.headers.get("Accept", ""),
            request.headers.get("X-GitHub-Api-Version", ""),
        )
        with self._response_lock:
            cached = (
                self._responses.get(key)
                if method == "GET" and "If-None-Match" not in request.headers
                else None
            )
        if cached:
            kwargs["headers"] = {**(kwargs.get("headers") or {}), "If-None-Match": cached[0]}
        attempt, quota_attempt = 0, 0
        while True:
            try:
                response = self.client.request(method, path, **kwargs)
                if response.status_code == 304 and cached:
                    return json.loads(cached[1])
                response.raise_for_status()
                data = response.json() if response.content else None
                if method == "GET":
                    with self._response_lock:
                        self._responses.pop(key, None)
                        if (
                            response.headers.get("ETag")
                            and response.content
                            and len(response.content) <= 2 * 1024 * 1024
                        ):
                            self._responses[key] = (response.headers["ETag"], response.content)
                            while (
                                len(self._responses) > self._max_responses
                                or sum(len(entry[1]) for entry in self._responses.values())
                                > self._max_response_bytes
                            ):
                                self._responses.popitem(last=False)
                return data
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                delay = (
                    rate_limit_delay(exc.response, quota_attempt)
                    if isinstance(exc, httpx.HTTPStatusError)
                    else None
                )
                if delay is not None:
                    self.wait_for_quota(exc.response, delay)
                    quota_attempt += 1
                    continue
                transient = isinstance(exc, httpx.TransportError) or exc.response.status_code in (
                    500,
                    502,
                    503,
                    504,
                )
                if method != "GET" or not transient or attempt == 2:
                    raise
                sleep(2**attempt)
                attempt += 1

    def paginate(self, path, params=None):
        for page in range(1, 101):
            batch = self.request(
                "GET", path, params={**(params or {}), "per_page": 100, "page": page}
            )
            yield from batch
            if len(batch) < 100:
                return
        raise ValueError("GitHub pagination limit reached; reconciliation incomplete")

    def find_issue(self, repo, marker):
        for issue in self.paginate(f"repos/{repo}/issues", {"state": "all"}):
            if "pull_request" not in issue and marker in (issue.get("body") or ""):
                return issue
        return None

    def create_issue(self, repo, title, body, labels=()):
        try:
            return self.request(
                "POST",
                f"repos/{repo}/issues",
                json={"title": title, "body": body, "labels": list(labels)},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                raise PublicationUncertain(
                    "GitHub may have created the issue; reconciliation required"
                ) from exc
            raise
        except httpx.TransportError as exc:
            raise PublicationUncertain(
                "Issue creation response lost; reconciliation required"
            ) from exc

    def ensure_labels(self, repo):
        labels = {
            "maxwell": "8250df",
            **{f"maxwell:factor:{name}": "bfdadc" for name in FACTORS},
            "maxwell:state:suspected": "fbca04",
            "maxwell:state:reviewed": "1d76db",
            "maxwell:state:audited": "5319e7",
            "maxwell:state:human-confirmed": "0e8a16",
            "maxwell:state:rejected": "eeeeee",
            **{
                f"maxwell:validation:{state}": color
                for state, color in (
                    ("pending", "fbca04"),
                    ("stale", "fbca04"),
                    ("conflicted", "d73a4a"),
                    ("accepted", "0e8a16"),
                    ("rejected", "d73a4a"),
                    ("inconclusive", "fbca04"),
                )
            },
            "maxwell:integration:verified": "0e8a16",
            **{f"maxwell:priority:P{i}": "d4c5f9" for i in range(4)},
            **{
                f"maxwell:{stage}:{verdict}": color
                for stage in ("review", "audit")
                for verdict, color in (
                    ("supported", "0e8a16"),
                    ("rejected", "eeeeee"),
                    ("inconclusive", "fbca04"),
                )
            },
        }
        existing = {label["name"] for label in self.paginate(f"repos/{repo}/labels")}
        for name, color in labels.items():
            if name in existing:
                continue
            try:
                self.request("POST", f"repos/{repo}/labels", json={"name": name, "color": color})
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 422:
                    raise

    def comments(self, repo, issue):
        return list(self.paginate(f"repos/{repo}/issues/{issue}/comments"))

    def create_comment(self, repo, issue, body):
        try:
            return self.request(
                "POST", f"repos/{repo}/issues/{issue}/comments", json={"body": body}
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                raise PublicationUncertain("Comment creation may have succeeded") from exc
            raise
        except httpx.TransportError as exc:
            raise PublicationUncertain("Comment creation response lost") from exc

    def upsert_comment(self, repo, issue, marker, body):
        for comment in self.comments(repo, issue):
            if marker in comment["body"]:
                if comment["body"] != body:
                    self.request(
                        "PATCH",
                        f"repos/{repo}/issues/comments/{comment['id']}",
                        json={"body": body},
                    )
                return
        self.request("POST", f"repos/{repo}/issues/{issue}/comments", json={"body": body})

    def mutate(self, path, payload, *, method="POST"):
        try:
            return self.request(method, path, json=payload)
        except httpx.TransportError as exc:
            raise PublicationUncertain(
                "GitHub mutation response lost; reconciliation required"
            ) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                raise PublicationUncertain(
                    "GitHub mutation may have succeeded; reconciliation required"
                ) from exc
            raise

    def pull(self, repo, number):
        return self.request("GET", f"repos/{repo}/pulls/{number}")

    def merge_pull(self, repo, number, head, method):
        return self.mutate(
            f"repos/{repo}/pulls/{number}/merge",
            {"sha": head, "merge_method": method},
            method="PUT",
        )

    def check_runs(self, repo, sha):
        for page in range(1, 101):
            batch = self.request(
                "GET",
                f"repos/{repo}/commits/{sha}/check-runs",
                params={"filter": "latest", "per_page": 100, "page": page},
            )["check_runs"]
            yield from batch
            if len(batch) < 100:
                return
        raise ValueError("GitHub check pagination limit reached; auto-merge evidence incomplete")

    def failed_required_checks(self, repo, number, head, base):
        query = """query MaxwellRepairChecks($owner: String!, $name: String!, $number: Int!, $cursor: String) {
          repository(owner: $owner, name: $name) {
            pullRequest(number: $number) {
              headRefOid baseRefOid
              commits(last: 1) { nodes { commit { statusCheckRollup {
                contexts(first: 100, after: $cursor) {
                  pageInfo { hasNextPage endCursor }
                  nodes {
                    ... on CheckRun {
                      databaseId name status conclusion detailsUrl summary text
                      isRequired(pullRequestNumber: $number)
                    }
                    ... on StatusContext {
                      context state description targetUrl
                      isRequired(pullRequestNumber: $number)
                    }
                  }
                }
              } } } }
            }
          }
        }"""
        owner, name = repo.split("/", 1)
        cursor, failures = None, []
        for _ in range(100):
            response = self.request(
                "POST",
                "graphql",
                json={
                    "query": query,
                    "variables": {"owner": owner, "name": name, "number": number, "cursor": cursor},
                },
            )
            if response.get("errors"):
                raise ValueError(
                    "Cannot read required CI checks: "
                    + str(response["errors"][0].get("message", "GraphQL error"))
                )
            try:
                pull = response["data"]["repository"]["pullRequest"]
                if (pull["headRefOid"], pull["baseRefOid"]) != (head, base):
                    raise ValueError(
                        "PR moved while reading required checks; synchronize and retry"
                    )
                rollup = pull["commits"]["nodes"][0]["commit"]["statusCheckRollup"]
                if rollup is None:
                    return []
                contexts = rollup["contexts"]
                for check in contexts["nodes"]:
                    if not check or not check.get("isRequired"):
                        continue
                    failed = (
                        check.get("status") == "COMPLETED"
                        and check.get("conclusion") in ("FAILURE", "TIMED_OUT", "STARTUP_FAILURE")
                    ) or check.get("state") in ("FAILURE", "ERROR")
                    if failed and len(failures) < 20:
                        failures.append(
                            {
                                "id": check.get("databaseId"),
                                "name": str(check.get("name") or check.get("context"))[:200],
                                "state": check.get("conclusion") or check.get("state"),
                                "url": str(check.get("detailsUrl") or check.get("targetUrl") or "")[
                                    :1000
                                ],
                                "summary": str(
                                    check.get("summary") or check.get("description") or ""
                                )[:800],
                                "text": str(check.get("text") or "")[:2000],
                            }
                        )
                page = contexts["pageInfo"]
                if not page["hasNextPage"]:
                    return sorted(failures, key=lambda item: (item["name"], str(item["id"])))
                following = page["endCursor"]
                if not following or following == cursor:
                    raise ValueError("Required-check pagination did not advance")
                cursor = following
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError("GitHub returned incomplete required-check evidence") from exc
        raise ValueError("Required-check pagination limit reached")

    def find_pull(self, repo, marker, branch):
        matches = list(
            self.paginate(
                f"repos/{repo}/pulls", {"state": "all", "head": f"{repo.split('/')[0]}:{branch}"}
            )
        )
        marked = [pull for pull in matches if marker in (pull.get("body") or "")]
        if len(marked) > 1 or any(marker not in (pull.get("body") or "") for pull in matches):
            raise ValueError("Repair branch already belongs to a conflicting or duplicate PR")
        return marked[0] if marked else None

    def create_pull(self, repo, title, body, branch, base):
        return self.mutate(
            f"repos/{repo}/pulls",
            {"title": title, "body": body, "head": branch, "base": base, "draft": True},
        )

    def set_draft(self, pull, draft):
        mutation = "convertPullRequestToDraft" if draft else "markPullRequestReadyForReview"
        data = self.mutate(
            "graphql",
            {
                "query": f"mutation($id: ID!) {{ {mutation}(input: {{pullRequestId: $id}}) {{ pullRequest {{ id isDraft }} }} }}",
                "variables": {"id": pull["node_id"]},
            },
        )
        if data.get("errors"):
            raise ValueError("GitHub draft transition failed: " + str(data["errors"])[:1000])

    def create_review(self, repo, number, head, body, comments):
        return self.mutate(
            f"repos/{repo}/pulls/{number}/reviews",
            {"commit_id": head, "event": "COMMENT", "body": body, "comments": comments},
        )
