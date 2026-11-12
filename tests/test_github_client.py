from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
import requests
from github import BadCredentialsException, GithubException, RateLimitExceededException

from agm_miner.config import Settings
from agm_miner.github.client import (
    GitHubAuthError,
    GitHubClient,
    GitHubRequestError,
    default_requester,
)

Response = tuple[dict[str, Any], Any] | Exception


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now

    def wall(self) -> float:
        return self.now


class FakeRequester:
    def __init__(self, responses: list[Response], clock: FakeClock) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict | None, Any, float]] = []
        self.clock = clock

    def requestJsonAndCheck(self, verb, url, parameters=None, headers=None, input=None):  # noqa: N802,A002
        self.calls.append((verb, url, parameters, input, self.clock.now))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def search_page(total: int, *items: tuple[int, str, str]) -> dict[str, Any]:
    return {
        "total_count": total,
        "incomplete_results": False,
        "items": [
            {
                "path": path,
                "repository": {"id": repo_id, "node_id": f"R_{repo_id}", "full_name": name},
            }
            for repo_id, name, path in items
        ],
    }


@pytest.fixture
def make_client() -> Callable[..., tuple[GitHubClient, FakeRequester, FakeClock]]:
    def factory(responses: list[Response], **settings: Any):
        clock = FakeClock()
        requester = FakeRequester(responses, clock)
        client = GitHubClient(
            Settings(DATABASE_URL="postgresql://unused", **settings),
            requester=requester,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            wall_clock=clock.wall,
        )
        return client, requester, clock

    return factory


def test_search_code_parses_hits(make_client) -> None:
    client, requester, _ = make_client([({}, search_page(2, (1, "a/b", "CLAUDE.md")))])
    page = client.search_code("filename:CLAUDE.md", page=1)
    assert page.total_count == 2
    assert page.hits[0].repo_id == 1 and page.hits[0].full_name == "a/b"
    assert page.hits[0].node_id == "R_1"
    assert requester.calls[0][2] == {"q": "filename:CLAUDE.md", "page": 1, "per_page": 100}


def test_code_search_requests_are_spaced(make_client) -> None:
    client, requester, _ = make_client([({}, search_page(0)), ({}, search_page(0))])
    client.search_code("q", page=1)
    client.search_code("q", page=2)
    first, second = requester.calls[0][-1], requester.calls[1][-1]
    assert second - first >= 6.5


def test_sleeps_until_reset_when_quota_is_low(make_client) -> None:
    low = {"X-RateLimit-Remaining": "1", "X-RateLimit-Reset": "1100"}
    client, requester, clock = make_client(
        [(low, search_page(0)), ({}, search_page(0))], RATE_LIMIT_SLEEP_BUFFER_SECONDS=5
    )
    client.search_code("q", page=1)
    client.search_code("q", page=2)
    assert requester.calls[1][-1] >= 1105


def test_retries_secondary_rate_limit_honouring_retry_after(make_client) -> None:
    limited = RateLimitExceededException(
        403, {"message": "You have exceeded a secondary rate limit"}, {"Retry-After": "30"}
    )
    client, requester, clock = make_client(
        [limited, ({}, search_page(0))], RATE_LIMIT_SLEEP_BUFFER_SECONDS=5
    )
    client.search_code("q", page=1)
    assert len(requester.calls) == 2
    assert 35 in clock.sleeps


def test_retries_transient_errors_with_bounded_attempts(make_client) -> None:
    errors = [GithubException(502, {"message": "Bad gateway"}, {}) for _ in range(3)]
    client, requester, clock = make_client(errors, GITHUB_MAX_RETRIES=2)
    with pytest.raises(GitHubRequestError, match="502"):
        client.graphql("query { viewer { login } }")
    assert len(requester.calls) == 3
    assert [s for s in clock.sleeps if s >= 30] == [30.0, 60.0]


def test_retries_search_timeouts(make_client) -> None:
    timeout = GithubException(408, {"message": "Request timed out while fetching results."}, {})
    client, requester, _ = make_client([timeout, ({}, search_page(0))])
    assert client.search_code("q", page=1).total_count == 0
    assert len(requester.calls) == 2


def test_retries_network_errors_with_bounded_attempts(make_client) -> None:
    errors = [requests.ConnectionError("connection reset") for _ in range(3)]
    client, requester, clock = make_client(errors, GITHUB_MAX_RETRIES=2)
    with pytest.raises(GitHubRequestError, match="ConnectionError"):
        client.graphql("query { viewer { login } }")
    assert len(requester.calls) == 3
    assert [s for s in clock.sleeps if s >= 30] == [30.0, 60.0]


@contextmanager
def github_stub(statuses: list[int]) -> Iterator[str]:
    """Local HTTP server answering each request with the next status (JSON body)."""
    pending = list(statuses)

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            status = pending.pop(0)
            body = json.dumps({"message": f"status {status}", "data": {}}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer  # noqa: N815 - http.server's names

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


@pytest.mark.parametrize("status", [502, 403])
def test_default_requester_surfaces_error_statuses_to_the_client(status: int) -> None:
    # PyGithub's own retry policy turned these into urllib3 RetryErrors, which skipped the
    # client's backoff (the select run of 2026-11-12 died on "too many 502 error responses").
    with github_stub([status]) as url:
        requester = default_requester("token", base_url=url)
        with pytest.raises(GithubException) as info:
            requester.requestJsonAndCheck("POST", "/graphql", input={"query": "{}"})
    assert info.value.status == status


def test_client_retries_a_real_502_and_succeeds() -> None:
    with github_stub([502, 200]) as url:
        clock = FakeClock()
        client = GitHubClient(
            Settings(DATABASE_URL="postgresql://unused"),
            requester=default_requester("token", base_url=url),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            wall_clock=clock.wall,
        )
        assert client.graphql("query { viewer { login } }")["message"] == "status 200"
    assert 30.0 in clock.sleeps


def test_every_wait_is_reported_with_its_reason(make_client) -> None:
    low = {"X-RateLimit-Remaining": "1", "X-RateLimit-Reset": "1100"}
    limited = RateLimitExceededException(
        403, {"message": "You have exceeded a secondary rate limit"}, {"Retry-After": "30"}
    )
    client, _, clock = make_client(
        [(low, search_page(0)), limited, ({}, search_page(0))],
        RATE_LIMIT_SLEEP_BUFFER_SECONDS=5,
    )
    waits: list[tuple[str, float]] = []

    @contextmanager
    def observe(reason: str, seconds: float):
        waits.append((reason, seconds))
        yield

    client.observe_waits(observe)
    client.search_code("q", page=1)
    client.search_code("q", page=2)

    reasons = [reason for reason, _ in waits]
    assert any("quota low (1 left)" in reason for reason in reasons)
    assert any("failed with 403; retry 1/" in reason for reason in reasons)
    assert any("pacing (6.5s between requests)" in reason for reason in reasons)
    # The observer sees exactly the sleeps the client makes.
    assert [seconds for _, seconds in waits] == clock.sleeps


def test_does_not_retry_client_errors(make_client) -> None:
    client, requester, _ = make_client([GithubException(422, {"message": "Invalid query"}, {})])
    with pytest.raises(GitHubRequestError, match="422"):
        client.search_code("bad", page=1)
    assert len(requester.calls) == 1


def test_bad_credentials_raise_auth_error(make_client) -> None:
    client, _, _ = make_client([BadCredentialsException(401, {"message": "Bad credentials"}, {})])
    with pytest.raises(GitHubAuthError):
        client.graphql("query { viewer { login } }")


def test_missing_token_is_reported() -> None:
    settings = Settings(DATABASE_URL="postgresql://unused", GITHUB_TOKEN=None)
    with pytest.raises(GitHubAuthError, match="GITHUB_TOKEN"):
        GitHubClient(settings)
