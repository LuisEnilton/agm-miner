"""RQ1 LLM backends: request translation, the OpenRouter HTTP client and the factory.

Nothing here reaches the network: FakeSession plays OpenRouter with canned responses."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agm_miner.cli import app
from agm_miner.config import Rq1Config, Settings
from agm_miner.rq1 import backends as backends_module
from agm_miner.rq1.backends import (
    OPENROUTER_PRIVACY_SETTINGS,
    AnthropicBackend,
    DailyLimitError,
    OpenRouterBackend,
    PromptRequest,
    list_free_structured_models,
    make_backend,
)
from agm_miner.rq1.llm import LLMAuthError, LLMClient, LLMRequestError, LLMResult
from agm_miner.rq1.store import BatchState, ResponseCache
from tests.test_rq1_foundations import FakeLLM

SCHEMA = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}


def prompt(custom_id: str = "p1") -> PromptRequest:
    return PromptRequest(custom_id, "You label sections.", "Section text", SCHEMA, 500, "labels")


# --------------------------------------------------------------------------- translation


def test_anthropic_params_cache_the_system_prompt_and_constrain_the_output() -> None:
    backend = AnthropicBackend(
        LLMClient(Settings(DATABASE_URL="postgresql://unused"), client=object()),
        "claude-sonnet-5-5",
        "low",
    )
    params = backend.params(prompt())
    assert params == {
        "model": "claude-sonnet-5-5",
        "max_tokens": 500,
        "system": [
            {
                "type": "text",
                "text": "You label sections.",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": "Section text"}],
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
    }


def test_anthropic_execute_goes_through_batches(tmp_path: Path) -> None:
    backend = AnthropicBackend(FakeLLM(), "claude-sonnet-5-5", "low")
    results = backend.execute(
        [prompt("a"), prompt("b")], ResponseCache(tmp_path), BatchState(tmp_path / "s.json")
    )
    assert sorted(results) == ["a", "b"] and backend.client.batches == {"batch_1": ["a", "b"]}


def test_openrouter_body() -> None:
    body = OpenRouterBackend("key", "nvidia/model:free").body(prompt())
    assert body["model"] == "nvidia/model:free" and body["max_tokens"] == 500
    assert body["messages"] == [
        {"role": "system", "content": "You label sections."},
        {"role": "user", "content": "Section text"},
    ]
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "labels", "strict": True, "schema": SCHEMA},
    }
    assert body["provider"] == {"require_parameters": True}


# --------------------------------------------------------------------------- OpenRouter


class FakeResponse:
    def __init__(self, status: int, data: Any, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.text = json.dumps(data)

    def json(self) -> Any:
        return self._data


def answer(content: str | None = '{"x": 1}', finish: str = "stop") -> FakeResponse:
    return FakeResponse(
        200,
        {
            "provider": "DeepInfra",
            "choices": [{"finish_reason": finish, "message": {"content": content}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4},
        },
    )


def error(status: int, message: str, headers: dict[str, str] | None = None) -> FakeResponse:
    return FakeResponse(status, {"error": {"code": status, "message": message}}, headers)


class FakeSession:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.posts: list[dict[str, Any]] = []

    def post(self, url: str, json: dict, headers: dict, timeout: float) -> FakeResponse:
        self.posts.append({"url": url, "json": json, "headers": headers})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url: str, timeout: float) -> FakeResponse:
        return self.responses.pop(0)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def openrouter(*responses: Any, **options: Any) -> tuple[OpenRouterBackend, FakeSession, Clock]:
    session, clock = FakeSession(*responses), Clock()
    backend = OpenRouterBackend(
        "secret-key",
        "nvidia/model:free",
        session=session,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **options,
    )
    return backend, session, clock


def test_openrouter_success_is_normalised() -> None:
    backend, session, _ = openrouter(answer())
    result = backend.complete(prompt())
    assert result == LLMResult(
        "p1",
        "succeeded",
        text='{"x": 1}',
        stop_reason="stop",
        usage={"input_tokens": 12, "output_tokens": 4},
        served_by="DeepInfra",
    )
    assert session.posts[0]["url"].endswith("/chat/completions")
    assert session.posts[0]["headers"] == {"Authorization": "Bearer secret-key"}


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (answer(finish="length"), "truncated"),
        (answer(finish="content_filter"), "refused"),
        (answer(content=None), "errored"),
        (FakeResponse(200, {"error": {"message": "upstream failed"}}), "errored"),
    ],
)
def test_openrouter_failed_answers(response: FakeResponse, status: str) -> None:
    backend, _, _ = openrouter(response)
    result = backend.complete(prompt())
    assert result.status == status and not result.ok


def test_openrouter_retries_rate_limits_and_outages_with_observable_waits() -> None:
    backend, session, clock = openrouter(
        error(429, "Rate limit exceeded: limit_rpm/nvidia", {"Retry-After": "7"}),
        error(502, "Bad gateway"),
        ConnectionError_(),
        answer(),
    )
    reasons: list[str] = []

    @contextmanager
    def observe(reason: str, seconds: float):
        reasons.append(reason)
        yield

    backend.observe_waits(observe)
    assert backend.complete(prompt()).ok
    assert len(session.posts) == 4
    assert clock.sleeps[0] == 7  # Retry-After honoured
    assert "OpenRouter answered 429; retry 1" in reasons
    assert "OpenRouter unreachable; retry 3" in reasons


def ConnectionError_() -> Exception:  # noqa: N802 - mimics the requests exception name
    import requests

    return requests.ConnectionError("connection reset")


def test_openrouter_gives_up_after_bounded_retries() -> None:
    backend, session, _ = openrouter(*[error(503, "overloaded")] * 3, max_retries=2)
    result = backend.complete(prompt())
    assert result.status == "errored" and "503" in result.error
    assert len(session.posts) == 3


def test_openrouter_paces_requests() -> None:
    backend, _, clock = openrouter(answer(), answer(), requests_per_minute=20)
    backend.complete(prompt("a"))
    backend.complete(prompt("b"))
    assert clock.sleeps == [3.0]


def test_openrouter_errors_that_stop_the_run() -> None:
    backend, _, _ = openrouter(error(401, "No auth credentials found"))
    with pytest.raises(LLMAuthError, match="OPENROUTER_API_KEY"):
        backend.complete(prompt())

    backend, _, _ = openrouter(
        error(404, "No endpoints found matching your data policy (Free model publication)")
    )
    with pytest.raises(LLMRequestError, match=OPENROUTER_PRIVACY_SETTINGS):
        backend.complete(prompt())

    backend, _, _ = openrouter(error(400, "This model does not support structured outputs"))
    with pytest.raises(LLMRequestError, match="400"):
        backend.complete(prompt())


def test_daily_limit_stops_and_the_next_run_continues(tmp_path: Path) -> None:
    cache, state = ResponseCache(tmp_path / "cache"), BatchState(tmp_path / "state.json")
    prompts = [prompt("a"), prompt("b"), prompt("c")]
    backend, session, _ = openrouter(
        answer(),
        error(429, "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000"),
    )
    with pytest.raises(DailyLimitError, match="run the same command again"):
        backend.execute(prompts, cache, state)
    assert cache.get("a") is not None and cache.get("b") is None

    # Next day: "a" comes from the cache; only "b" and "c" are asked.
    backend, session, _ = openrouter(answer(), answer())
    results = backend.execute(prompts, cache, state)
    assert all(result.ok for result in results.values())
    assert len(session.posts) == 2


def test_free_structured_models_listing() -> None:
    models = {
        "data": [
            {
                "id": "a/big:free",
                "name": "Big",
                "context_length": 9,
                "supported_parameters": ["structured_outputs", "response_format"],
            },
            {"id": "a/plain:free", "supported_parameters": ["response_format"]},
            {"id": "a/paid", "supported_parameters": ["structured_outputs"]},
        ]
    }
    assert list_free_structured_models(FakeSession(FakeResponse(200, models))) == [
        {"id": "a/big:free", "name": "Big", "context_length": 9}
    ]


# --------------------------------------------------------------------------- factory and CLI


def test_make_backend_follows_the_configured_provider() -> None:
    settings = Settings(DATABASE_URL="postgresql://unused", OPENROUTER_API_KEY="k")
    claude = make_backend(settings, Rq1Config(), "classify")
    assert isinstance(claude, AnthropicBackend) and claude.model == "claude-sonnet-5-5"
    assert claude.effort == "low"

    config = Rq1Config(provider="openrouter", propose_model="nvidia/model:free")
    free = make_backend(settings, config, "propose")
    assert isinstance(free, OpenRouterBackend) and free.model == "nvidia/model:free"

    with pytest.raises(LLMAuthError, match="OPENROUTER_API_KEY"):
        make_backend(Settings(DATABASE_URL="postgresql://unused"), config, "propose")


def test_smoke_command(monkeypatch) -> None:
    class Echo:
        provider, model = "openrouter", "nvidia/model:free"

        def complete(self, request: PromptRequest) -> LLMResult:
            assert request.schema["required"] == ["ok", "echo"]
            return LLMResult("smoke", "succeeded", text='{"ok": true, "echo": "agm"}')

    monkeypatch.setattr(backends_module, "make_backend", lambda *args: Echo())
    result = CliRunner().invoke(app, ["rq1", "smoke"])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output[result.output.index("{") :])
    assert summary["schema_ok"] is True and summary["model"] == "nvidia/model:free"
