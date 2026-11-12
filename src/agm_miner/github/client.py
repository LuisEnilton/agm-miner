"""Rate-limit-aware access to the GitHub REST (code search) and GraphQL APIs.

Every request goes through :meth:`GitHubClient._request`, which

* spaces requests of the same bucket by a minimum interval (the code-search endpoint allows only
  10 requests per minute; exceeding it trips GitHub's secondary rate limits / abuse detection),
* tracks the remaining quota per bucket from the ``x-ratelimit-*`` response headers and sleeps
  until the reset when it drops under the configured threshold,
* retries rate-limit errors, transient 408/5xx responses and network errors with bounded
  exponential backoff (``Retry-After`` is honoured), and fails loudly after
  ``GITHUB_MAX_RETRIES``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any, Protocol

import requests
from github import Auth, BadCredentialsException, Github, GithubException

from agm_miner.config import Settings

logger = logging.getLogger(__name__)

CODE_SEARCH = "code_search"
GRAPHQL = "graphql"

_MIN_INTERVAL_SECONDS = {CODE_SEARCH: 6.5, GRAPHQL: 1.0}
_BACKOFF_BASE_SECONDS = 30.0
_BACKOFF_MAX_SECONDS = 900.0
_TRANSIENT_STATUSES = frozenset({408, 500, 502, 503, 504})
_NETWORK_ERRORS = (requests.ConnectionError, requests.Timeout)


class GitHubAuthError(RuntimeError):
    """The token is missing, invalid or lacks permissions."""


class GitHubRequestError(RuntimeError):
    """A request kept failing after all retries."""


class Requester(Protocol):
    def requestJsonAndCheck(  # noqa: N802 - PyGithub's name
        self,
        verb: str,
        url: str,
        parameters: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        input: Any | None = None,  # noqa: A002 - PyGithub's name
    ) -> tuple[dict[str, Any], Any]: ...


@dataclass(slots=True)
class _BucketState:
    remaining: int | None = None
    reset_at: float | None = None  # epoch seconds
    last_request: float | None = None  # monotonic seconds


@dataclass(frozen=True, slots=True)
class CodeSearchHit:
    repo_id: int
    node_id: str
    full_name: str
    path: str


@dataclass(frozen=True, slots=True)
class CodeSearchPage:
    total_count: int
    incomplete_results: bool
    hits: list[CodeSearchHit]


def default_requester(token: str, *, base_url: str | None = None) -> Requester:
    """PyGithub requester without its own retries: every error status reaches
    :meth:`GitHubClient._request` as a ``GithubException``. (A ``GithubRetry(total=0)`` would
    not do: its status list makes urllib3 raise ``RetryError`` on the first 5xx instead.)"""
    options: dict[str, Any] = {"base_url": base_url} if base_url else {}
    github = Github(
        auth=Auth.Token(token), retry=0, seconds_between_requests=None, timeout=60, **options
    )
    return github.requester


class GitHubClient:
    def __init__(
        self,
        settings: Settings,
        *,
        requester: Requester | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        if requester is None:
            if settings.github_token is None:
                raise GitHubAuthError("GITHUB_TOKEN is not configured (see .env.sample).")
            requester = default_requester(settings.github_token.get_secret_value())
        self._requester = requester
        self._sleep = sleep
        self._wait_observer: Callable[[str, float], AbstractContextManager[Any]] = (
            lambda reason, seconds: nullcontext()
        )
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._buckets: dict[str, _BucketState] = {}
        self._thresholds = {
            CODE_SEARCH: settings.search_rate_limit_min_remaining,
            GRAPHQL: settings.graphql_rate_limit_min_remaining,
        }

    # ------------------------------------------------------------------ public API

    def observe_waits(self, observer: Callable[[str, float], AbstractContextManager[Any]]) -> None:
        """Wrap every wait (pacing, low quota, retry back-off) in ``observer(reason, seconds)``,
        e.g. ``run.progress.waiting``, so the progress shows why the stage is idle."""
        self._wait_observer = observer

    def search_code(self, query: str, page: int, per_page: int = 100) -> CodeSearchPage:
        """One page (1-based) of ``GET /search/code``."""
        data = self._request(
            CODE_SEARCH,
            "GET",
            "/search/code",
            parameters={"q": query, "page": page, "per_page": per_page},
        )
        hits = [
            CodeSearchHit(
                repo_id=int(item["repository"]["id"]),
                node_id=item["repository"]["node_id"],
                full_name=item["repository"]["full_name"],
                path=item["path"],
            )
            for item in data.get("items", [])
        ]
        return CodeSearchPage(
            total_count=int(data.get("total_count", 0)),
            incomplete_results=bool(data.get("incomplete_results", False)),
            hits=hits,
        )

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Raw GraphQL response (``data`` and possibly partial ``errors``)."""
        return self._request(
            GRAPHQL, "POST", "/graphql", payload={"query": query, "variables": variables or {}}
        )

    # ------------------------------------------------------------------ plumbing

    def _request(
        self,
        bucket: str,
        verb: str,
        url: str,
        *,
        parameters: dict[str, Any] | None = None,
        payload: Any | None = None,
    ) -> Any:
        state = self._buckets.setdefault(bucket, _BucketState())
        max_retries = self._settings.github_max_retries
        for attempt in range(max_retries + 1):
            self._pace(bucket, state)
            try:
                headers, data = self._requester.requestJsonAndCheck(
                    verb, url, parameters=parameters, input=payload
                )
            except BadCredentialsException as exc:
                raise GitHubAuthError("GitHub rejected the token (check GITHUB_TOKEN).") from exc
            except GithubException as exc:
                self._record_headers(state, exc.headers or {})
                if attempt == max_retries or not self._is_retryable(exc):
                    raise GitHubRequestError(
                        f"{verb} {url} failed with status {exc.status}: {exc.data!r}"
                    ) from exc
                self._back_off(bucket, exc.status, self._retry_delay(exc, attempt), attempt)
                continue
            except _NETWORK_ERRORS as exc:
                if attempt == max_retries:
                    raise GitHubRequestError(f"{verb} {url} failed: {exc!r}") from exc
                self._back_off(bucket, type(exc).__name__, self._backoff(attempt), attempt)
                continue
            self._record_headers(state, headers)
            return data
        raise AssertionError("unreachable")

    def _back_off(self, bucket: str, cause: object, delay: float, attempt: int) -> None:
        max_retries = self._settings.github_max_retries
        logger.warning(
            "GitHub %s request failed with %s (attempt %s/%s); retrying in %.0fs",
            bucket,
            cause,
            attempt + 1,
            max_retries,
            delay,
        )
        self._wait(
            f"GitHub {bucket} request failed with {cause}; retry {attempt + 1}/{max_retries}",
            delay,
        )

    def _pace(self, bucket: str, state: _BucketState) -> None:
        interval = _MIN_INTERVAL_SECONDS.get(bucket, 0.0)
        if state.last_request is not None and interval:
            wait = state.last_request + interval - self._monotonic()
            if wait > 0:
                self._wait(f"GitHub {bucket} pacing ({interval:g}s between requests)", wait)
        threshold = self._thresholds.get(bucket, 0)
        if (
            state.remaining is not None
            and state.reset_at is not None
            and state.remaining <= threshold
        ):
            wait = (
                state.reset_at - self._wall_clock() + self._settings.rate_limit_sleep_buffer_seconds
            )
            if wait > 0:
                logger.warning(
                    "GitHub %s quota low (%s left); sleeping %.0fs until reset",
                    bucket,
                    state.remaining,
                    wait,
                )
                self._wait(f"GitHub {bucket} quota low ({state.remaining} left)", wait)
            state.remaining = None
        state.last_request = self._monotonic()

    def _wait(self, reason: str, seconds: float) -> None:
        with self._wait_observer(reason, seconds):
            self._sleep(seconds)

    @staticmethod
    def _record_headers(state: _BucketState, headers: dict[str, Any]) -> None:
        lowered = {key.lower(): value for key, value in headers.items()}
        if "x-ratelimit-remaining" in lowered:
            state.remaining = int(lowered["x-ratelimit-remaining"])
        if "x-ratelimit-reset" in lowered:
            state.reset_at = float(lowered["x-ratelimit-reset"])

    @staticmethod
    def _is_retryable(exc: GithubException) -> bool:
        if exc.status in _TRANSIENT_STATUSES or exc.status == 429:
            return True
        if exc.status == 403:
            message = str(exc.data).lower()
            headers = {key.lower() for key in (exc.headers or {})}
            return "rate limit" in message or "abuse" in message or "retry-after" in headers
        return False

    def _retry_delay(self, exc: GithubException, attempt: int) -> float:
        headers = {key.lower(): value for key, value in (exc.headers or {}).items()}
        if "retry-after" in headers:
            return float(headers["retry-after"]) + self._settings.rate_limit_sleep_buffer_seconds
        if headers.get("x-ratelimit-remaining") == "0" and "x-ratelimit-reset" in headers:
            wait = float(headers["x-ratelimit-reset"]) - self._wall_clock()
            return max(wait, 0.0) + self._settings.rate_limit_sleep_buffer_seconds
        return self._backoff(attempt)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(_BACKOFF_BASE_SECONDS * 2**attempt, _BACKOFF_MAX_SECONDS)
