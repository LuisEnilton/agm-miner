"""LLM providers behind one interface, so the RQ1 stages never depend on where models run.

* :class:`AnthropicBackend` (final collection): Claude through the official SDK, bulk work via the
  Message Batches API (``execution.execute_batched``), the system prompt cached.
* :class:`OpenRouterBackend` (testing the pipeline, e.g. with free models): OpenRouter's
  ``chat/completions`` over plain HTTP, one request at a time at the account's pace. Each answer is
  cached as soon as it arrives, so a run stopped by the daily limit of free models continues
  where it stopped on the next day.

Stages build provider-neutral :class:`PromptRequest` objects; each backend translates them.
Results are :class:`~agm_miner.rq1.llm.LLMResult` either way.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from agm_miner.config import Rq1Config, Settings
from agm_miner.progress import Progress
from agm_miner.rq1.execution import execute_batched
from agm_miner.rq1.llm import LLMAuthError, LLMClient, LLMRequest, LLMRequestError, LLMResult
from agm_miner.rq1.store import BatchState, ResponseCache

OPENROUTER_API = "https://openrouter.ai/api/v1"
OPENROUTER_PRIVACY_SETTINGS = "https://openrouter.ai/settings/privacy"

WaitObserver = Callable[[str, float], AbstractContextManager[Any]]


@dataclass(frozen=True, slots=True)
class PromptRequest:
    """One provider-neutral request: instructions, the content and the JSON schema to answer."""

    custom_id: str  # cache key of the answer
    system: str
    user: str
    schema: dict[str, Any]
    max_tokens: int = 16_000
    schema_name: str = "answer"


class LLMBackend(Protocol):
    provider: str
    model: str

    def observe_waits(self, observer: WaitObserver) -> None: ...

    def complete(self, request: PromptRequest) -> LLMResult: ...

    def execute(
        self,
        prompts: Sequence[PromptRequest],
        cache: ResponseCache,
        state: BatchState,
        progress: Progress | None = None,
    ) -> dict[str, LLMResult]: ...


# --------------------------------------------------------------------------- Anthropic


class AnthropicBackend:
    provider = "anthropic"

    def __init__(self, client: LLMClient, model: str, effort: str) -> None:
        self.client = client
        self.model = model
        self.effort = effort

    def params(self, request: PromptRequest) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": request.max_tokens,
            # Identical across a phase's requests: cached after the first one.
            "system": [
                {"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": request.user}],
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": request.schema},
            },
        }

    def observe_waits(self, observer: WaitObserver) -> None:
        self.client.observe_waits(observer)

    def complete(self, request: PromptRequest) -> LLMResult:
        return self.client.complete(LLMRequest(request.custom_id, self.params(request)))

    def execute(
        self,
        prompts: Sequence[PromptRequest],
        cache: ResponseCache,
        state: BatchState,
        progress: Progress | None = None,
    ) -> dict[str, LLMResult]:
        translated = [LLMRequest(request.custom_id, self.params(request)) for request in prompts]
        return execute_batched(self.client, translated, cache, state, progress)


# --------------------------------------------------------------------------- OpenRouter


class DailyLimitError(LLMRequestError):
    """The account reached the daily request limit of free models; answers so far are saved."""


_RETRYABLE = frozenset({408, 429, 500, 502, 503, 504})
_BACKOFF_BASE_SECONDS = 5.0
_BACKOFF_MAX_SECONDS = 300.0


class OpenRouterBackend:
    provider = "openrouter"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        requests_per_minute: int = 20,
        max_retries: int = 4,
        timeout: float = 600.0,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._interval = 60.0 / requests_per_minute
        self._max_retries = max_retries
        self._timeout = timeout
        self._session = session or requests.Session()
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request: float | None = None
        self._wait_observer: WaitObserver = lambda reason, seconds: nullcontext()

    def observe_waits(self, observer: WaitObserver) -> None:
        self._wait_observer = observer

    def body(self, request: PromptRequest) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "max_tokens": request.max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.schema,
                },
            },
            # Route only to endpoints that honour response_format.
            "provider": {"require_parameters": True},
        }

    def execute(
        self,
        prompts: Sequence[PromptRequest],
        cache: ResponseCache,
        state: BatchState,
        progress: Progress | None = None,
    ) -> dict[str, LLMResult]:
        """Answer each request (from the cache when possible), caching every success at once.

        Raises :class:`DailyLimitError` when the daily quota runs out; everything answered until
        then is cached, so running the same command later continues from there.
        """
        progress = progress or Progress()
        progress.set_total("requests", len(prompts))
        results: dict[str, LLMResult] = {}
        for request in prompts:
            cached = cache.get(request.custom_id)
            if cached is not None:
                results[request.custom_id] = cached
                progress.advance("requests")
                progress.advance("cached")
                continue
            progress.set_phase(f"asking {self.model}")
            result = self.complete(request)
            cache.put(result)
            results[request.custom_id] = result
            progress.advance("requests")
            progress.advance(result.status)
        return results

    def complete(self, request: PromptRequest) -> LLMResult:
        for attempt in range(self._max_retries + 1):
            self._pace()
            try:
                response = self._session.post(
                    f"{OPENROUTER_API}/chat/completions",
                    json=self.body(request),
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                if attempt == self._max_retries:
                    return LLMResult(request.custom_id, "errored", error=f"network: {exc}")
                self._wait(f"OpenRouter unreachable; retry {attempt + 1}", self._backoff(attempt))
                continue
            if response.status_code == 200:
                return self._result(request.custom_id, response)
            message = _error_message(response)
            if response.status_code in (401, 403):
                raise LLMAuthError(f"OpenRouter rejected OPENROUTER_API_KEY: {message}")
            if response.status_code == 429 and "per-day" in message.replace(" ", "-").lower():
                raise DailyLimitError(
                    f"OpenRouter daily limit reached ({message}). Answers so far are saved: run "
                    "the same command again after the limit resets."
                )
            if response.status_code == 404 and "data policy" in message.lower():
                raise LLMRequestError(
                    f"OpenRouter found no endpoint for {self.model} under your data policy. "
                    f"Allow the model's endpoints in {OPENROUTER_PRIVACY_SETTINGS} ({message})"
                )
            if response.status_code not in _RETRYABLE:
                raise LLMRequestError(
                    f"OpenRouter request failed with status {response.status_code}: {message}"
                )
            if attempt == self._max_retries:
                return LLMResult(
                    request.custom_id, "errored", error=f"{response.status_code}: {message}"
                )
            delay = _retry_after(response) or self._backoff(attempt)
            self._wait(f"OpenRouter answered {response.status_code}; retry {attempt + 1}", delay)
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ plumbing

    def _result(self, custom_id: str, response: Any) -> LLMResult:
        data = response.json()
        served_by = data.get("provider")
        if data.get("error"):
            return LLMResult(custom_id, "errored", error=str(data["error"]), served_by=served_by)
        choice = (data.get("choices") or [{}])[0]
        finish = choice.get("finish_reason")
        text = (choice.get("message") or {}).get("content")
        usage = data.get("usage") or {}
        status = {"length": "truncated", "content_filter": "refused"}.get(finish, "succeeded")
        if status == "succeeded" and not text:
            status = "errored"
        return LLMResult(
            custom_id,
            status,
            text=text,
            stop_reason=finish,
            usage={
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
            },
            error=None if status == "succeeded" else f"finish_reason={finish}",
            served_by=served_by,
        )

    def _pace(self) -> None:
        if self._last_request is not None:
            wait = self._last_request + self._interval - self._monotonic()
            if wait > 0:
                self._wait(f"OpenRouter pacing ({self._interval:g}s between requests)", wait)
        self._last_request = self._monotonic()

    def _wait(self, reason: str, seconds: float) -> None:
        with self._wait_observer(reason, seconds):
            self._sleep(seconds)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(_BACKOFF_BASE_SECONDS * 2**attempt, _BACKOFF_MAX_SECONDS)


def _error_message(response: Any) -> str:
    try:
        error = response.json().get("error") or {}
    except ValueError:
        return response.text[:300]
    if isinstance(error, dict):
        metadata = error.get("metadata") or {}
        raw = metadata.get("raw") if isinstance(metadata, dict) else None
        return " ".join(str(part) for part in (error.get("message"), raw) if part)
    return str(error)


def _retry_after(response: Any) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def list_free_structured_models(session: Any | None = None) -> list[dict[str, Any]]:
    """Free OpenRouter models whose endpoints accept structured outputs (public, no key)."""
    response = (session or requests.Session()).get(f"{OPENROUTER_API}/models", timeout=60)
    if response.status_code != 200:
        raise LLMRequestError(f"OpenRouter model list failed with status {response.status_code}")
    models = []
    for model in response.json().get("data", []):
        parameters = set(model.get("supported_parameters") or [])
        if model["id"].endswith(":free") and "structured_outputs" in parameters:
            models.append(
                {
                    "id": model["id"],
                    "name": model.get("name"),
                    "context_length": model.get("context_length"),
                }
            )
    return sorted(models, key=lambda model: model["id"])


# --------------------------------------------------------------------------- factory


def make_backend(settings: Settings, config: Rq1Config, phase: str) -> LLMBackend:
    """Backend for ``phase`` (propose, consolidate or classify) of ``config.provider``."""
    model, effort = config.model_for(phase)
    if config.provider == "anthropic":
        return AnthropicBackend(LLMClient(settings), model, effort)
    if settings.openrouter_api_key is None:
        raise LLMAuthError("rq1.provider is openrouter but OPENROUTER_API_KEY is not set in .env")
    return OpenRouterBackend(
        settings.openrouter_api_key.get_secret_value(),
        model,
        requests_per_minute=config.openrouter_requests_per_minute,
    )
