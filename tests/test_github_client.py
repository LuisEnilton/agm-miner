from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from github import BadCredentialsException, GithubException, RateLimitExceededException

from agm_miner.config import Settings
from agm_miner.github.client import (
    GitHubAuthError,
    GitHubClient,
    GitHubRequestError,
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
