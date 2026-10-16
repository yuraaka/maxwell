from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest

from maxwell.github import GitHub, PublicationUncertain, rate_limit_delay, rate_limit_message


@pytest.fixture(autouse=True)
def clear_response_cache():
    GitHub._responses.clear()
    yield
    GitHub._responses.clear()


@pytest.fixture
def quota_clock(monkeypatch):
    clock = [0]
    monkeypatch.setattr("maxwell.github.time", lambda: 1000 + clock[0])
    monkeypatch.setattr("maxwell.github.monotonic", lambda: clock[0])

    def advance(event, delay):
        clock[0] += delay
        return event.is_set()

    monkeypatch.setattr("maxwell.github.Event.wait", advance)
    return clock


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_primary_quota_wait_retains_and_retries_request(home, monkeypatch, quota_clock, method):
    messages, requests = [], []
    client = GitHub(home.load().github, on_status=messages.append)

    def send(request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1060"},
                request=request,
            )
        return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr(client.client, "send", send)
    try:
        assert client.request(method, "repos/acme/repo/issues", json={"title": "Finding"}) == {
            "ok": True
        }
        assert quota_clock[0] == 61
        assert len(requests) == 2
        assert requests[0].content == requests[1].content
        assert "primary quota (HTTP 403, remaining 0)" in messages[0]
        assert "retry at 1970-01-01 00:17:41 UTC" in messages[0]
        assert "job retained" in messages[0]
        assert "31s left" in messages[1]
        assert "retrying the same request" in messages[-1]
    finally:
        client.close()


@pytest.mark.parametrize("status", [403, 429])
def test_secondary_quota_wait_obeys_retry_after_and_keeps_job(
    home, monkeypatch, quota_clock, status
):
    client = GitHub(home.load().github)
    calls = []

    def send(request, **kwargs):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status, headers={"retry-after": "45"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr(client.client, "send", send)
    try:
        assert client.request("GET", "user") == {"ok": True}
        assert quota_clock[0] == 45 and len(calls) == 2
    finally:
        client.close()


def test_wait_can_be_interrupted_without_retrying_request(home, monkeypatch):
    client = GitHub(home.load().github)
    requests = []

    def send(request, **kwargs):
        requests.append(request)
        return httpx.Response(429, headers={"retry-after": "300"}, request=request)

    def interrupt(delay):
        client.close()
        return True

    monkeypatch.setattr(client.client, "send", send)
    monkeypatch.setattr(client._closed, "wait", interrupt)
    with pytest.raises(ValueError, match="stopped while waiting for quota"):
        client.request("GET", "user")
    assert len(requests) == 1


def test_quota_retries_are_not_limited_to_three_attempts(home, monkeypatch, quota_clock):
    client = GitHub(home.load().github)
    calls = []

    def send(request, **kwargs):
        calls.append(request)
        if len(calls) <= 5:
            return httpx.Response(429, headers={"retry-after": "1"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr(client.client, "send", send)
    try:
        assert client.request("GET", "user") == {"ok": True}
        assert quota_clock[0] == 5 and len(calls) == 6
    finally:
        client.close()


def test_close_interrupts_real_quota_wait_in_background_thread(home, monkeypatch):
    waiting = Event()
    client = GitHub(home.load().github, on_status=lambda message: waiting.set())
    monkeypatch.setattr(
        client.client,
        "send",
        lambda request, **kwargs: httpx.Response(
            429, headers={"retry-after": "300"}, request=request
        ),
    )
    with ThreadPoolExecutor() as executor:
        future = executor.submit(client.request, "GET", "user")
        try:
            assert waiting.wait(3)
            client.close()
            with pytest.raises(ValueError, match="stopped while waiting for quota"):
                future.result(timeout=3)
        finally:
            client.close()


def test_quota_backoff_distinguishes_permissions_and_handles_missing_headers():
    assert (
        rate_limit_delay(httpx.Response(403, json={"message": "Resource not accessible"}), 0)
        is None
    )
    for attempt, delay in ((0, 60), (1, 120), (5, 900)):
        assert (
            rate_limit_delay(
                httpx.Response(403, json={"message": "You have exceeded a secondary rate limit"}),
                attempt,
            )
            == delay
        )
    response = httpx.Response(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1791815799"}
    )
    assert "resets at 2026-10-12 14:36:39 UTC" in rate_limit_message(response)


def test_conditional_reads_revalidate_across_clients_and_observe_changes(home, monkeypatch):
    settings = home.load().github
    version, charged, requests = 1, [], []

    def send(client, request, **kwargs):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"id": 1}, request=request)
        etag = f'"version-{version}"'
        if request.headers.get("If-None-Match") == etag:
            return httpx.Response(304, request=request)
        charged.append(request)
        return httpx.Response(
            200, json={"version": version}, headers={"ETag": etag}, request=request
        )

    monkeypatch.setattr(httpx.Client, "send", send)
    for expected in (1, 1, 2):
        version = expected
        client = GitHub(settings)
        try:
            result = client.request("GET", "repos/acme/repo/issues", params={"page": 1})
            assert result == {"version": expected}
            result["version"] = "modified locally"
            if expected == 2:
                client.create_comment("acme/repo", 1, "Assessment")
        finally:
            client.close()
    assert len(charged) == 2
    assert requests[1].headers["If-None-Match"] == '"version-1"'
    assert requests[2].headers["If-None-Match"] == '"version-1"'


def test_conditional_cache_isolated_by_account_and_page_and_never_hides_errors(home, monkeypatch):
    settings = home.load().github
    requests = []
    status = 200

    def send(client, request, **kwargs):
        requests.append(request)
        return httpx.Response(status, json={"ok": True}, headers={"ETag": '"one"'}, request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    first = GitHub(settings)
    second = GitHub(settings.model_copy(update={"token": type(settings.token)("other-account")}))
    try:
        first.request("GET", "user", params={"page": 1})
        second.request("GET", "user", params={"page": 1})
        first.request("GET", "user", params={"page": 2})
        assert all("If-None-Match" not in request.headers for request in requests)
        status = 403
        with pytest.raises(httpx.HTTPStatusError):
            first.request("GET", "user", params={"page": 1})
        assert requests[-1].headers["If-None-Match"] == '"one"'
    finally:
        first.close()
        second.close()


def test_label_setup_creates_only_missing_labels(home, github):
    client = GitHub(home.load().github)
    try:
        client.ensure_labels("acme/repo")
        assert "maxwell:validation:conflicted" in github["labels"]
        writes = sum(method == "POST" for method, _, _ in github["requests"])
        assert writes > 0
        client.ensure_labels("acme/repo")
        assert sum(method == "POST" for method, _, _ in github["requests"]) == writes
        del github["labels"]["maxwell:state:reviewed"]
        client.ensure_labels("acme/repo")
        assert sum(method == "POST" for method, _, _ in github["requests"]) == writes + 1
    finally:
        client.close()


def test_conditional_cache_is_bounded_and_reloads_evicted_pages(home, monkeypatch):
    requests = []
    client = GitHub(home.load().github)
    monkeypatch.setattr(GitHub, "_max_responses", 2)

    def send(request, **kwargs):
        requests.append(request)
        return httpx.Response(200, json={"ok": True}, headers={"ETag": '"one"'}, request=request)

    monkeypatch.setattr(client.client, "send", send)
    try:
        for page in (1, 2, 3, 1):
            client.request("GET", "user", params={"page": page})
        assert len(GitHub._responses) == 2
        assert "If-None-Match" not in requests[-1].headers
    finally:
        client.close()


@pytest.mark.parametrize("failure", [500, 502, 503, 504, "transport"])
def test_transient_reads_retry_with_bounded_backoff(home, monkeypatch, failure):
    client = GitHub(home.load().github)
    requests, waits = [], []

    def send(request, **kwargs):
        requests.append(request)
        if len(requests) < 3:
            if failure == "transport":
                raise httpx.ReadError("Response lost", request=request)
            return httpx.Response(failure, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr(client.client, "send", send)
    monkeypatch.setattr("maxwell.github.sleep", waits.append)
    try:
        assert client.request("GET", "user") == {"ok": True}
        assert len(requests) == 3 and waits == [1, 2]
    finally:
        client.close()


@pytest.mark.parametrize("status,attempts", [(401, 1), (403, 1), (404, 1), (502, 3)])
def test_failed_reads_stop_after_allowed_attempts(home, monkeypatch, status, attempts):
    client = GitHub(home.load().github)
    requests = []

    def send(request, **kwargs):
        requests.append(request)
        return httpx.Response(status, request=request)

    monkeypatch.setattr(client.client, "send", send)
    monkeypatch.setattr("maxwell.github.sleep", lambda seconds: None)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            client.request("GET", "user")
        assert len(requests) == attempts
    finally:
        client.close()


@pytest.mark.parametrize("kind", ["issue", "comment"])
def test_ambiguous_creations_are_never_retried_by_http_client(home, monkeypatch, kind):
    client = GitHub(home.load().github)
    requests = []

    def send(request, **kwargs):
        requests.append(request)
        return httpx.Response(502, request=request)

    monkeypatch.setattr(client.client, "send", send)
    monkeypatch.setattr("maxwell.github.sleep", lambda seconds: pytest.fail("POST was retried"))
    try:
        with pytest.raises(PublicationUncertain):
            if kind == "issue":
                client.create_issue("acme/repo", "Finding", "Evidence")
            else:
                client.create_comment("acme/repo", 1, "Assessment")
        assert len(requests) == 1
    finally:
        client.close()
