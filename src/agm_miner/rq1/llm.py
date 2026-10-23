"""Thin wrapper over the Anthropic SDK for the RQ1 coding: Message Batches (50% cheaper,
asynchronous) for bulk work and single streamed requests for one-off calls.

Results are normalised into :class:`LLMResult` (plain data), so the rest of the package and the
tests never depend on SDK types. The SDK client is injectable, and every wait goes through
``observe_waits`` like the GitHub client, so progress shows why the command is idle.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Any

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from agm_miner.config import Settings

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


class LLMAuthError(RuntimeError):
    """The Claude API rejected (or could not find) the credentials."""


class LLMRequestError(RuntimeError):
    """A Claude API call failed in a way retrying will not fix."""


@dataclass(frozen=True, slots=True)
class LLMRequest:
    custom_id: str  # [a-zA-Z0-9_-]{1,64}; the cache key of the answer
    params: dict[str, Any]  # Messages API parameters (model, max_tokens, system, messages, ...)


@dataclass(frozen=True, slots=True)
class LLMResult:
    custom_id: str
    # succeeded | refused | truncated (max_tokens) | errored | canceled | expired
    status: str
    text: str | None = None
    stop_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    # Upstream provider that actually answered (OpenRouter routes a model to several).
    served_by: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    def as_dict(self) -> dict[str, Any]:
        return {
            "custom_id": self.custom_id,
            "status": self.status,
            "text": self.text,
            "stop_reason": self.stop_reason,
            "usage": dict(self.usage),
            "error": self.error,
            "served_by": self.served_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LLMResult:
        return cls(**data)


def result_from_message(custom_id: str, message: Any) -> LLMResult:
    """Normalise a ``Message``: the text block, the stop reason and the token usage."""
    text = next((block.text for block in message.content if block.type == "text"), None)
    status = {"refusal": "refused", "max_tokens": "truncated"}.get(message.stop_reason, "succeeded")
    usage = {name: int(getattr(message.usage, name, 0) or 0) for name in _USAGE_FIELDS}
    return LLMResult(custom_id, status, text=text, stop_reason=message.stop_reason, usage=usage)


@dataclass(frozen=True, slots=True)
class BatchProgress:
    ended: bool
    counts: dict[str, int]  # processing / succeeded / errored / canceled / expired


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        *,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = 30.0,
    ) -> None:
        if client is None:
            key = settings.anthropic_api_key
            # Without ANTHROPIC_API_KEY the SDK still resolves other credentials (e.g. a profile
            # from `ant auth login`); a missing credential surfaces as LLMAuthError on first use.
            client = anthropic.Anthropic(api_key=key.get_secret_value()) if key else None
        self._client = client
        self._sleep = sleep
        self._poll_seconds = poll_seconds
        self._wait_observer: Callable[[str, float], AbstractContextManager[Any]] = (
            lambda reason, seconds: nullcontext()
        )

    def observe_waits(self, observer: Callable[[str, float], AbstractContextManager[Any]]) -> None:
        self._wait_observer = observer

    @property
    def sdk(self) -> Any:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    # ------------------------------------------------------------------ batches

    def submit_batch(self, requests: Sequence[LLMRequest]) -> str:
        batch = self._call(
            self.sdk.messages.batches.create,
            requests=[
                Request(
                    custom_id=request.custom_id,
                    params=MessageCreateParamsNonStreaming(**request.params),
                )
                for request in requests
            ],
        )
        return batch.id

    def batch_progress(self, batch_id: str) -> BatchProgress:
        batch = self._call(self.sdk.messages.batches.retrieve, batch_id)
        counts = batch.request_counts
        return BatchProgress(
            ended=batch.processing_status == "ended",
            counts={
                name: int(getattr(counts, name, 0) or 0)
                for name in ("processing", "succeeded", "errored", "canceled", "expired")
            },
        )

    def wait_for_batch(self, batch_id: str, on_progress: Callable[[BatchProgress], None]) -> None:
        while True:
            progress = self.batch_progress(batch_id)
            on_progress(progress)
            if progress.ended:
                return
            reason = (
                f"Claude batch {batch_id}: {progress.counts.get('processing', 0)} "
                "requests still processing"
            )
            with self._wait_observer(reason, self._poll_seconds):
                self._sleep(self._poll_seconds)

    def batch_results(self, batch_id: str) -> Iterator[LLMResult]:
        for entry in self._call(self.sdk.messages.batches.results, batch_id):
            outcome = entry.result
            if outcome.type == "succeeded":
                yield result_from_message(entry.custom_id, outcome.message)
            elif outcome.type == "errored":
                yield LLMResult(entry.custom_id, "errored", error=str(outcome.error))
            else:  # canceled / expired: safe to submit again
                yield LLMResult(entry.custom_id, outcome.type)

    # ------------------------------------------------------------------ single requests

    def complete(self, request: LLMRequest) -> LLMResult:
        """One streamed request (no timeout risk for long outputs)."""

        def stream() -> Any:
            with self.sdk.messages.stream(**request.params) as response:
                return response.get_final_message()

        return result_from_message(request.custom_id, self._call(stream))

    def count_tokens(self, params: dict[str, Any]) -> int:
        allowed = {"model", "messages", "system", "output_config", "thinking", "tools"}
        counted = self._call(
            self.sdk.messages.count_tokens,
            **{key: value for key, value in params.items() if key in allowed},
        )
        return int(counted.input_tokens)

    # ------------------------------------------------------------------ errors

    @staticmethod
    def _call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        # The SDK already retries 408/409/429/5xx and connection errors (max_retries).
        try:
            return function(*args, **kwargs)
        except TypeError as exc:
            # Raised by the SDK on the first request when no credential source resolves.
            if "authentication method" not in str(exc):
                raise
            raise LLMAuthError(
                "No Claude API credentials: set ANTHROPIC_API_KEY in .env or run `ant auth login`."
            ) from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMAuthError(f"Claude API rejected the credentials: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMRequestError(
                f"Claude API request failed with status {exc.status_code}: {exc.message}"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMRequestError(f"Could not reach the Claude API: {exc}") from exc
