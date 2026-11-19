"""Gemini backend (testing phase only): the official ``google-genai`` SDK, one request at a time.

The SDK is the optional ``gemini`` extra (``poetry install -E gemini``): the final collection used
Anthropic, so a replication does not need it. Importing this module without the extra raises an
:class:`~agm_miner.rq1.llm.LLMRequestError` that says how to install it.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from typing import Any

import httpx
import httpx2

from agm_miner.progress import Progress
from agm_miner.rq1.backends import (
    _BACKOFF_BASE_SECONDS,
    _BACKOFF_MAX_SECONDS,
    DailyLimitError,
    PromptRequest,
    WaitObserver,
    execute_one_by_one,
)
from agm_miner.rq1.llm import LLMAuthError, LLMRequestError, LLMResult
from agm_miner.rq1.store import BatchState, ResponseCache

try:
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types as genai_types
except ImportError as exc:  # pragma: no cover - depends on the installed extras
    raise LLMRequestError(
        "the Gemini provider needs the optional google-genai SDK: run `poetry install -E gemini` "
        "(or `pip install agm-miner[gemini]`)"
    ) from exc


GEMINI_RATE_LIMITS = "https://aistudio.google.com/rate-limit"
_GEMINI_FINISH_STATUS = {
    "STOP": "succeeded",
    "MAX_TOKENS": "truncated",
    "SAFETY": "refused",
    "RECITATION": "refused",
    "BLOCKLIST": "refused",
    "PROHIBITED_CONTENT": "refused",
    "SPII": "refused",
}


class GeminiBackend:
    provider = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        requests_per_minute: int = 10,
        max_retries: int = 4,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model = model
        self._client = client or genai.Client(api_key=api_key)
        self._interval = 60.0 / requests_per_minute
        self._max_retries = max_retries
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request: float | None = None
        self._wait_observer: WaitObserver = lambda reason, seconds: nullcontext()

    def observe_waits(self, observer: WaitObserver) -> None:
        self._wait_observer = observer

    def config(self, request: PromptRequest) -> genai_types.GenerateContentConfig:
        return genai_types.GenerateContentConfig(
            system_instruction=request.system,
            response_mime_type="application/json",
            response_json_schema=request.schema,
            max_output_tokens=request.max_tokens,
        )

    def execute(
        self,
        prompts: Sequence[PromptRequest],
        cache: ResponseCache,
        state: BatchState,
        progress: Progress | None = None,
    ) -> dict[str, LLMResult]:
        return execute_one_by_one(self, prompts, cache, progress)

    def complete(self, request: PromptRequest) -> LLMResult:
        for attempt in range(self._max_retries + 1):
            self._pace()
            try:
                response = self._client.models.generate_content(
                    model=self.model, contents=request.user, config=self.config(request)
                )
            except genai_errors.APIError as exc:
                delay = self._handle_error(exc)
                reason = f"Gemini answered {exc.code}"
            except (httpx.HTTPError, httpx2.HTTPError) as exc:
                delay, reason = None, f"Gemini unreachable ({type(exc).__name__})"
            else:
                return self._result(request.custom_id, response)
            if attempt == self._max_retries:
                return LLMResult(request.custom_id, "errored", error=reason)
            self._wait(f"{reason}; retry {attempt + 1}", delay or self._backoff(attempt))
        raise AssertionError("unreachable")

    def _handle_error(self, exc: genai_errors.APIError) -> float | None:
        """Raise for errors that stop the run; return the delay suggested for retryable ones."""
        details = _gemini_details(exc)
        if exc.code in (401, 403) or (exc.code == 400 and "API key" in (exc.message or "")):
            raise LLMAuthError(f"Gemini rejected GEMINI_API_KEY: {exc.message}")
        if exc.code == 429:
            quotas = " ".join(
                str(violation.get("quotaId", ""))
                for detail in details
                for violation in detail.get("violations", [])
            )
            if "PerDay" in quotas:
                raise DailyLimitError(
                    f"Gemini daily limit of {self.model} reached ({exc.message}). Answers so far "
                    "are saved: run the same command again after the limit resets (midnight "
                    f"Pacific time). Limits of your project: {GEMINI_RATE_LIMITS}"
                )
            return _retry_delay(details)
        if exc.code == 404:
            raise LLMRequestError(
                f"Gemini has no model {self.model!r} for this key ({exc.message}). Check "
                "rq1.*_model in study.yaml against the models of the free tier."
            )
        if exc.code is not None and exc.code >= 500:
            return None
        raise LLMRequestError(f"Gemini request failed with {exc.code} {exc.status}: {exc.message}")

    def _result(self, custom_id: str, response: Any) -> LLMResult:
        served_by = getattr(response, "model_version", None)
        candidates = response.candidates or []
        if not candidates:
            feedback = getattr(response, "prompt_feedback", None)
            reason = getattr(feedback, "block_reason", None) if feedback else None
            return LLMResult(
                custom_id, "refused", error=f"prompt blocked: {reason}", served_by=served_by
            )
        finish = getattr(candidates[0].finish_reason, "name", str(candidates[0].finish_reason))
        status = _GEMINI_FINISH_STATUS.get(finish, "errored")
        try:
            text = response.text
        except ValueError:  # no text part (e.g. blocked)
            text = None
        if status == "succeeded" and not text:
            status = "errored"
        usage = response.usage_metadata
        return LLMResult(
            custom_id,
            status,
            text=text,
            stop_reason=finish,
            usage={
                "input_tokens": int(getattr(usage, "prompt_token_count", 0) or 0),
                # Thinking tokens are billed (and count against limits) as output.
                "output_tokens": int(getattr(usage, "candidates_token_count", 0) or 0)
                + int(getattr(usage, "thoughts_token_count", 0) or 0),
            },
            error=None if status == "succeeded" else f"finish_reason={finish}",
            served_by=served_by,
        )

    def _pace(self) -> None:
        if self._last_request is not None:
            wait = self._last_request + self._interval - self._monotonic()
            if wait > 0:
                self._wait(f"Gemini pacing ({self._interval:g}s between requests)", wait)
        self._last_request = self._monotonic()

    def _wait(self, reason: str, seconds: float) -> None:
        with self._wait_observer(reason, seconds):
            self._sleep(seconds)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(_BACKOFF_BASE_SECONDS * 2**attempt, _BACKOFF_MAX_SECONDS)


def _gemini_details(exc: genai_errors.APIError) -> list[dict[str, Any]]:
    body = exc.details if isinstance(exc.details, dict) else {}
    error = body.get("error", body) if isinstance(body, dict) else {}
    details = error.get("details") if isinstance(error, dict) else None
    return [detail for detail in details or [] if isinstance(detail, dict)]


def _retry_delay(details: list[dict[str, Any]]) -> float | None:
    for detail in details:
        match = re.fullmatch(r"([\d.]+)s", str(detail.get("retryDelay", "")))
        if match:
            return float(match.group(1))
    return None


def list_gemini_models(client: Any) -> list[dict[str, Any]]:
    """Gemini models that generate content for this key (which ones are free is shown in AI
    Studio, not by the API)."""
    try:
        models = list(client.models.list())
    except genai_errors.APIError as exc:
        if exc.code in (400, 401, 403):
            raise LLMAuthError(f"Gemini rejected GEMINI_API_KEY: {exc.message}") from exc
        raise LLMRequestError(f"Gemini model list failed with {exc.code}: {exc.message}") from exc
    except (httpx.HTTPError, httpx2.HTTPError) as exc:
        raise LLMRequestError(f"Could not reach the Gemini API: {exc}") from exc
    return sorted(
        (
            {
                "id": model.name.removeprefix("models/"),
                "name": model.display_name,
                "input_token_limit": model.input_token_limit,
                "output_token_limit": model.output_token_limit,
            }
            for model in models
            if "generateContent" in (model.supported_actions or [])
        ),
        key=lambda model: model["id"],
    )
