"""Gemini backend (testing provider): request translation, answers, errors and limits.

Nothing reaches the network: FakeGenai plays the google-genai client with canned responses and
the SDK's own error classes."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google.genai import errors, types

from agm_miner.config import Rq1Config, Settings
from agm_miner.rq1.backends import (
    DailyLimitError,
    GeminiBackend,
    list_gemini_models,
    make_backend,
)
from agm_miner.rq1.llm import LLMAuthError, LLMRequestError
from agm_miner.rq1.store import BatchState, ResponseCache
from tests.test_rq1_backends import SCHEMA, Clock, prompt
from tests.test_rq1_foundations import A, B, C


def response(
    text: str | None = '{"x": 1}',
    finish: types.FinishReason = types.FinishReason.STOP,
    candidates: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        candidates=[SimpleNamespace(finish_reason=finish)] if candidates else [],
        text=text,
        usage_metadata=SimpleNamespace(
            prompt_token_count=10, candidates_token_count=5, thoughts_token_count=7
        ),
        model_version="gemini-3.5-flash-lite-001",
        prompt_feedback=SimpleNamespace(block_reason="SAFETY") if not candidates else None,
    )


def api_error(code: int, message: str, *details: dict[str, Any]) -> errors.APIError:
    body = {"error": {"code": code, "message": message, "status": "X", "details": list(details)}}
    return (errors.ServerError if code >= 500 else errors.ClientError)(code, body)


PER_MINUTE = {
    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
    "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}],
}
PER_DAY = {
    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
    "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
}
RETRY_32S = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "32s"}


class FakeGenai:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.models = self

    def generate_content(self, model: str, contents: str, config: Any) -> Any:
        self.calls.append({"model": model, "contents": contents, "config": config})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def gemini(*outcomes: Any, **options: Any) -> tuple[GeminiBackend, FakeGenai, Clock]:
    client, clock = FakeGenai(*outcomes), Clock()
    backend = GeminiBackend(
        "key",
        "gemini-3.5-flash-lite",
        client=client,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **options,
    )
    return backend, client, clock


def test_request_translation() -> None:
    backend, client, _ = gemini(response())
    backend.complete(prompt())
    call = client.calls[0]
    assert call["model"] == "gemini-3.5-flash-lite" and call["contents"] == "Section text"
    config = call["config"]
    assert config.system_instruction == "You label sections."
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == SCHEMA and config.max_output_tokens == 500


def test_answer_is_normalised_with_thinking_tokens_as_output() -> None:
    backend, _, _ = gemini(response())
    result = backend.complete(prompt())
    assert result.ok and result.text == '{"x": 1}' and result.stop_reason == "STOP"
    assert result.usage == {"input_tokens": 10, "output_tokens": 12}
    assert result.served_by == "gemini-3.5-flash-lite-001"


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        (response(finish=types.FinishReason.MAX_TOKENS), "truncated"),
        (response(finish=types.FinishReason.SAFETY), "refused"),
        (response(candidates=False), "refused"),
        (response(text=""), "errored"),
        (response(finish=types.FinishReason.OTHER), "errored"),
    ],
)
def test_failed_answers(outcome: Any, status: str) -> None:
    backend, _, _ = gemini(outcome)
    result = backend.complete(prompt())
    assert result.status == status and not result.ok


def test_per_minute_quota_waits_the_suggested_delay() -> None:
    backend, client, clock = gemini(
        api_error(429, "Quota exceeded", PER_MINUTE, RETRY_32S),
        api_error(503, "The model is overloaded"),
        httpx.ConnectError("connection reset"),
        response(),
    )
    reasons: list[str] = []

    @contextmanager
    def observe(reason: str, seconds: float):
        reasons.append(reason)
        yield

    backend.observe_waits(observe)
    assert backend.complete(prompt()).ok and len(client.calls) == 4
    assert clock.sleeps[0] == 32
    assert "Gemini answered 429; retry 1" in reasons
    assert "Gemini unreachable (ConnectError); retry 3" in reasons


def test_gives_up_after_bounded_retries() -> None:
    backend, client, _ = gemini(*[api_error(503, "overloaded")] * 3, max_retries=2)
    result = backend.complete(prompt())
    assert result.status == "errored" and "503" in result.error and len(client.calls) == 3


def test_paces_requests() -> None:
    backend, _, clock = gemini(response(), response(), requests_per_minute=10)
    backend.complete(prompt(A))
    backend.complete(prompt(B))
    assert clock.sleeps == [6.0]


def test_errors_that_stop_the_run() -> None:
    for error, kind, message in [
        (api_error(400, "API key not valid. Please pass a valid API key."), LLMAuthError, "KEY"),
        (api_error(403, "Permission denied"), LLMAuthError, "GEMINI_API_KEY"),
        (api_error(404, "models/x is not found"), LLMRequestError, "study.yaml"),
        (api_error(400, "Invalid JSON schema"), LLMRequestError, "400"),
    ]:
        backend, _, _ = gemini(error)
        with pytest.raises(kind, match=message):
            backend.complete(prompt())


def test_daily_quota_stops_and_the_next_run_continues(tmp_path: Path) -> None:
    cache, state = ResponseCache(tmp_path / "cache"), BatchState(tmp_path / "state.json")
    prompts = [prompt(A), prompt(B), prompt(C)]
    backend, _, _ = gemini(response(), api_error(429, "Quota exceeded", PER_DAY, RETRY_32S))
    with pytest.raises(DailyLimitError, match="midnight Pacific time"):
        backend.execute(prompts, cache, state)
    assert cache.get(A) is not None and cache.get(B) is None

    backend, client, _ = gemini(response(), response())
    assert all(result.ok for result in backend.execute(prompts, cache, state).values())
    assert len(client.calls) == 2


def test_model_listing() -> None:
    def model(name: str, actions: list[str]) -> SimpleNamespace:
        return SimpleNamespace(
            name=f"models/{name}",
            display_name=name.title(),
            input_token_limit=1000,
            output_token_limit=100,
            supported_actions=actions,
        )

    client = SimpleNamespace(
        models=SimpleNamespace(
            list=lambda: [
                model("gemini-3.5-flash-lite", ["generateContent", "countTokens"]),
                model("gemini-embedding-2", ["embedContent"]),
            ]
        )
    )
    assert [m["id"] for m in list_gemini_models(client)] == ["gemini-3.5-flash-lite"]

    def broken():
        raise api_error(403, "Permission denied")

    with pytest.raises(LLMAuthError):
        list_gemini_models(SimpleNamespace(models=SimpleNamespace(list=broken)))


def test_make_backend_for_gemini(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config = Rq1Config(provider="gemini", classify_model="gemini-3.5-flash-lite")
    backend = make_backend(
        Settings(_env_file=None, DATABASE_URL="postgresql://unused", GEMINI_API_KEY="k"),
        config,
        "classify",
    )
    assert isinstance(backend, GeminiBackend) and backend.model == "gemini-3.5-flash-lite"
    with pytest.raises(LLMAuthError, match="GEMINI_API_KEY"):
        make_backend(
            Settings(_env_file=None, DATABASE_URL="postgresql://unused"), config, "classify"
        )
