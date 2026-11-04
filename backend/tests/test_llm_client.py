"""LLM 客户端行为：错误分类与重试策略（issue #17）。

openai SDK 异常全部离线构造（httpx.Request/Response 不发网络请求），
分类映射逐项断言；重试经脚本化的 SDK 桩（chat.completions.create 按序
抛异常/返回响应），退避睡眠替换为记录调用——只测客户端自身的策略。
"""

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace

import httpx
import openai
import pytest

from app.config import settings
from app.llm import (
    LLMContentError,
    LLMTransientError,
    PolishLLMClient,
    classify_llm_error,
    is_configured,
)

BASE_URL = "https://llm.example/v1"
API_KEY = "test-operator-key"
MODEL = "test-model"


def _request() -> httpx.Request:
    return httpx.Request("POST", f"{BASE_URL}/chat/completions")


def _status_error(cls: type[openai.APIStatusError], code: int) -> openai.APIStatusError:
    return cls(f"HTTP {code}", response=httpx.Response(code, request=_request()), body=None)


@pytest.mark.parametrize(
    "exc",
    [
        # 瞬时：连接失败/超时（APIConnectionError 全部子类）、408/409/429/5xx
        openai.APIConnectionError(request=_request()),
        openai.APITimeoutError(request=_request()),
        _status_error(openai.RateLimitError, 429),
        _status_error(openai.ConflictError, 409),
        _status_error(openai.APIStatusError, 408),
        _status_error(openai.InternalServerError, 500),
        _status_error(openai.InternalServerError, 503),
    ],
)
def test_classify_returns_transient_for_retryable_errors(exc: Exception) -> None:
    classified = classify_llm_error(exc)
    assert type(classified) is LLMTransientError
    assert classified.__cause__ is exc


@pytest.mark.parametrize(
    "exc",
    [
        # 内容性：其余 4xx（key/请求/配额问题重试不可能成功）；未知异常不盲目重试
        _status_error(openai.BadRequestError, 400),
        _status_error(openai.AuthenticationError, 401),
        _status_error(openai.PermissionDeniedError, 403),
        _status_error(openai.NotFoundError, 404),
        _status_error(openai.UnprocessableEntityError, 422),
        RuntimeError("意外异常"),
    ],
)
def test_classify_raises_content_error_without_retry_hope(exc: Exception) -> None:
    with pytest.raises(LLMContentError) as exc_info:
        classify_llm_error(exc)
    assert exc_info.value.__cause__ is exc


class _StubCompletions:
    """chat.completions 桩：按脚本顺序抛异常或返回响应，记录调用。"""

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[list[dict[str, str]]] = []

    async def create(self, *, model: str, messages: list[dict[str, str]]) -> object:
        assert model == MODEL
        self.calls.append(messages)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _client_with(outcomes: list[object]) -> tuple[PolishLLMClient, _StubCompletions]:
    completions = _StubCompletions(outcomes)
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return PolishLLMClient(sdk, MODEL), completions  # type: ignore[arg-type]


def _response(content: str | None, *, n_choices: int = 1) -> object:
    choices = [SimpleNamespace(message=SimpleNamespace(content=content)) for _ in range(n_choices)]
    return SimpleNamespace(choices=choices)


@pytest.fixture()
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """把退避睡眠换成记录延迟（不真等）。"""
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("app.llm.asyncio.sleep", fake_sleep)
    yield delays


def test_polish_retries_transient_errors_with_backoff_then_succeeds(
    instant_backoff: list[float],
) -> None:
    client, completions = _client_with(
        [
            openai.RateLimitError("429", response=httpx.Response(429, request=_request()), body=None),
            openai.InternalServerError("500", response=httpx.Response(500, request=_request()), body=None),
            _response("润色后的文本"),
        ]
    )

    result = asyncio.run(client.polish("原文", system_prompt="系统提示词"))

    assert result == "润色后的文本"
    assert len(completions.calls) == 3  # 3 次尝试：两次瞬时 + 一次成功
    assert instant_backoff == [2.0, 4.0]  # 指数退避：base * 2^(n-1)
    # 单段落调用：system 提示词在前、段落文本在后
    assert completions.calls[0] == [
        {"role": "system", "content": "系统提示词"},
        {"role": "user", "content": "原文"},
    ]


def test_polish_content_error_fails_immediately_without_retry(
    instant_backoff: list[float],
) -> None:
    client, completions = _client_with([_status_error(openai.AuthenticationError, 401)])

    with pytest.raises(LLMContentError):
        asyncio.run(client.polish("原文", system_prompt="p"))

    assert len(completions.calls) == 1  # 内容性错误不重试
    assert instant_backoff == []


def test_polish_transient_error_raises_after_exhausting_retries(
    instant_backoff: list[float],
) -> None:
    outcome = openai.RateLimitError("429", response=httpx.Response(429, request=_request()), body=None)
    client, completions = _client_with([outcome, outcome, outcome])

    with pytest.raises(LLMTransientError):
        asyncio.run(client.polish("原文", system_prompt="p"))

    assert len(completions.calls) == 3
    assert instant_backoff == [2.0, 4.0]


@pytest.mark.parametrize("response", [_response(None), _response("   "), _response("x", n_choices=0)])
def test_polish_empty_output_is_a_content_error(response: object) -> None:
    client, completions = _client_with([response])

    with pytest.raises(LLMContentError):
        asyncio.run(client.polish("原文", system_prompt="p"))

    assert len(completions.calls) == 1


# ---- complete(messages)：通用对话接缝（issue #32）——纪律与 polish 同一套 ----


def test_complete_retries_transient_errors_with_backoff_then_succeeds(
    instant_backoff: list[float],
) -> None:
    outcome = openai.RateLimitError("429", response=httpx.Response(429, request=_request()), body=None)
    client, completions = _client_with([outcome, outcome, _response("标记结果")])
    messages = [
        {"role": "system", "content": "系统提示词"},
        {"role": "user", "content": "待标记段落"},
    ]

    result = asyncio.run(client.complete(messages))

    assert result == "标记结果"
    assert len(completions.calls) == 3  # 两次瞬时 + 一次成功
    assert instant_backoff == [2.0, 4.0]
    # messages 原样透传：分块上下文由调用方组装，客户端不拆不拼
    assert completions.calls[0] == messages


def test_complete_content_error_fails_immediately_without_retry(
    instant_backoff: list[float],
) -> None:
    client, completions = _client_with([_status_error(openai.AuthenticationError, 401)])

    with pytest.raises(LLMContentError):
        asyncio.run(client.complete([{"role": "user", "content": "原文"}]))

    assert len(completions.calls) == 1  # 内容性错误不重试
    assert instant_backoff == []


def test_complete_transient_error_raises_after_exhausting_retries(
    instant_backoff: list[float],
) -> None:
    outcome = openai.InternalServerError("500", response=httpx.Response(500, request=_request()), body=None)
    client, completions = _client_with([outcome, outcome, outcome])

    with pytest.raises(LLMTransientError):
        asyncio.run(client.complete([{"role": "user", "content": "原文"}]))

    assert len(completions.calls) == 3
    assert instant_backoff == [2.0, 4.0]


def test_complete_empty_output_is_a_content_error(instant_backoff: list[float]) -> None:
    client, completions = _client_with([_response("  ")])

    with pytest.raises(LLMContentError):
        asyncio.run(client.complete([{"role": "user", "content": "原文"}]))

    assert len(completions.calls) == 1


def test_from_settings_reads_env_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "llm_base_url", BASE_URL)
    monkeypatch.setattr(settings, "llm_api_key", API_KEY)
    monkeypatch.setattr(settings, "llm_model", MODEL)

    client = PolishLLMClient.from_settings()

    assert client.model_name == MODEL
    assert is_configured() is True


def test_from_settings_refuses_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for attr in ("llm_base_url", "llm_api_key", "llm_model"):
        monkeypatch.setattr(settings, attr, "")
    assert is_configured() is False
    with pytest.raises(ValueError, match="模型接入未配置"):
        PolishLLMClient.from_settings()
