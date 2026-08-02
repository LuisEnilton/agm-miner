from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from functools import wraps
from typing import Any, Callable, TypeVar

from github import (
    Auth,
    BadCredentialsException,
    Github,
    GithubException,
    GithubRetry,
    RateLimitExceededException,
    UnknownObjectException,
)

from agm_miner.config import Settings


logger = logging.getLogger(__name__)
F = TypeVar("F", bound=Callable[..., Any])

CORE_BUCKET = "core"
CODE_SEARCH_BUCKET = "code_search"
FALLBACK_SLEEP_SECONDS = 60


class GitHubAuthError(Exception):
    """Raised when the GitHub token is invalid or missing permissions."""


class GitHubRepositoryNotFoundError(Exception):
    """Raised when a repository cannot be found on GitHub."""


def rate_limited(bucket_name: str) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(self: "GitHubClient", *args: Any, **kwargs: Any) -> Any:
            self._sleep_if_needed(bucket_name)
            try:
                return func(self, *args, **kwargs)
            except RateLimitExceededException:
                logger.warning(
                    "GitHub %s rate limit exceeded unexpectedly; sleeping and retrying once.",
                    bucket_name,
                )
                self._sleep_until_reset(bucket_name, force_refresh=True)
                return func(self, *args, **kwargs)
            except BadCredentialsException as exc:
                raise GitHubAuthError(
                    "GitHub authentication failed. Verify GITHUB_TOKEN in .env."
                ) from exc

        return wrapper  # type: ignore[return-value]

    return decorator


class GitHubClient:
    def __init__(self, settings: Settings, *, validate_token: bool = True) -> None:
        self.settings = settings
        no_retry = GithubRetry(total=0)
        self.github = Github(
            auth=Auth.Token(settings.github_token),
            retry=no_retry,
            seconds_between_requests=0.25,
            per_page=100,
        )
        if validate_token:
            self._validate_token()

    def _validate_token(self) -> None:
        try:
            login = self.github.get_user().login
            logger.info("GitHub token validated for user %s", login)
        except BadCredentialsException as exc:
            raise GitHubAuthError(
                "GitHub authentication failed. Verify GITHUB_TOKEN in .env."
            ) from exc

    def _threshold_for_bucket(self, bucket_name: str) -> int:
        if bucket_name == CODE_SEARCH_BUCKET:
            return self.settings.search_rate_limit_min_remaining
        return self.settings.core_rate_limit_min_remaining

    def _get_rate_resource(self, bucket_name: str) -> Any:
        overview = self.github.get_rate_limit()
        resources = getattr(overview, "resources", None)
        if resources is not None:
            return getattr(resources, bucket_name)
        return getattr(overview, bucket_name)

    def _sleep_if_needed(self, bucket_name: str) -> None:
        try:
            rate = self._get_rate_resource(bucket_name)
        except (RateLimitExceededException, AttributeError, GithubException) as exc:
            logger.warning(
                "Unable to read GitHub %s rate limit (%s). Sleeping %s seconds.",
                bucket_name,
                exc,
                FALLBACK_SLEEP_SECONDS,
            )
            time.sleep(FALLBACK_SLEEP_SECONDS)
            return

        if rate.remaining <= self._threshold_for_bucket(bucket_name):
            self._sleep_until_reset(bucket_name, rate.reset)

    def _sleep_until_reset(
        self,
        bucket_name: str,
        reset_at: datetime | None = None,
        force_refresh: bool = False,
    ) -> None:
        if reset_at is None or force_refresh:
            try:
                reset_at = self._get_rate_resource(bucket_name).reset
            except (AttributeError, GithubException) as exc:
                logger.warning(
                    "Unable to read GitHub %s reset time (%s). Sleeping %s seconds.",
                    bucket_name,
                    exc,
                    FALLBACK_SLEEP_SECONDS,
                )
                time.sleep(FALLBACK_SLEEP_SECONDS)
                return

        now = datetime.now(UTC)
        wait_seconds = max(
            0,
            int((reset_at.astimezone(UTC) - now).total_seconds())
            + self.settings.rate_limit_sleep_buffer_seconds,
        )
        if wait_seconds > 0:
            logger.warning(
                "GitHub %s rate limit low. Sleeping for %s seconds until %s.",
                bucket_name,
                wait_seconds,
                reset_at,
            )
            time.sleep(wait_seconds)

    @rate_limited(CODE_SEARCH_BUCKET)
    def search_code(self, query: str):
        logger.info("Searching GitHub code with query: %s", query)
        return self.github.search_code(query)

    @rate_limited(CORE_BUCKET)
    def get_repository(self, full_name: str):
        try:
            return self.github.get_repo(full_name)
        except UnknownObjectException as exc:
            raise GitHubRepositoryNotFoundError(
                f"Repository not found on GitHub: {full_name}"
            ) from exc

    @rate_limited(CORE_BUCKET)
    def get_contents(self, full_name: str, path: str = ""):
        try:
            return self.github.get_repo(full_name).get_contents(path)
        except UnknownObjectException as exc:
            raise GitHubRepositoryNotFoundError(
                f"Path not found in repository {full_name}: {path}"
            ) from exc

    @rate_limited(CORE_BUCKET)
    def get_commits(self, full_name: str, path: str):
        repo = self.github.get_repo(full_name)
        return repo.get_commits(path=path)

    @rate_limited(CORE_BUCKET)
    def get_commits_by_author(self, full_name: str, author: str):
        repo = self.github.get_repo(full_name)
        return repo.get_commits(author=author)

    @rate_limited(CORE_BUCKET)
    def get_commit(self, full_name: str, sha: str):
        repo = self.github.get_repo(full_name)
        return repo.get_commit(sha)

    @rate_limited(CORE_BUCKET)
    def count_commits(self, full_name: str) -> int:
        repo = self.github.get_repo(full_name)
        return repo.get_commits().totalCount

    @rate_limited(CORE_BUCKET)
    def count_contributors(self, full_name: str) -> int:
        repo = self.github.get_repo(full_name)
        return repo.get_contributors().totalCount
