"""LLM 客户端：openai 兼容的 LLM 调用边界（ADR-0005）。

调用方（Worker、Word Formatter 服务层、测试假客户端的替换目标）只需要知道
:class:`PolishLLMClient` 的 ``model_name`` 与 :meth:`PolishLLMClient.polish` /
:meth:`PolishLLMClient.complete`——base_url/api_key/model 全部来自 .env
（ADR-0005，无数据库配置路径），SDK 细节与重试策略都藏在本模块内部：

- **错误分类**：瞬时错误（网络失败/超时、HTTP 408/409/429/5xx）按指数退避
  重试至多 ``_MAX_ATTEMPTS`` 次；内容性错误（其余 4xx：key 无效、请求不合法、
  余额耗尽等）立即失败——重试不可能成功，只会拖延任务。未知异常同样视为
  内容性：对不了解的异常重试等于盲赌。分类逻辑单独成函数
  :func:`classify_llm_error`，对 SDK 异常类型的映射有直接测试。
- **重试策略唯一归口**：SDK 自身的自动重试被显式关闭（max_retries=0），
  否则退避时长与次数会被 SDK 暗中翻倍。退避用 asyncio.sleep，等待期间
  调用方不得持有任何数据库事务（app.db 短事务纪律）。

错误对外统一为本模块的 :class:`LLMTransientError`（可重试）与
:class:`LLMContentError`（立即失败），调用方无需接触 openai 异常类型。
"""

import asyncio

import openai
from openai import AsyncOpenAI

from app.config import Settings, settings

# 瞬时错误的重试上限与退避基数：第 n 次失败后等待 base * 2^(n-1) 秒。
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 2.0

# 这些状态码重试有意义：请求超时/冲突/限流；5xx 是服务端临时故障。
_TRANSIENT_STATUS_CODES = frozenset({408, 409, 429})


class LLMError(Exception):
    """LLM 调用失败基类。"""


class LLMTransientError(LLMError):
    """瞬时失败（网络/限流/5xx）：已按策略重试仍失败，或等待下次重试。"""


class LLMContentError(LLMError):
    """内容性失败（key/请求/配额等）：立即失败，重试不可能成功。"""


def classify_llm_error(exc: Exception) -> LLMTransientError:
    """按可重试性处置一次调用抛出的异常：瞬时错误包装为
    :class:`LLMTransientError` 返回（原异常挂 __cause__），交由调用方决定
    重试节奏；内容性错误当场抛 :class:`LLMContentError`——重试不可能成功，
    不给调用方"再试一次"的错觉。

    映射：连接失败/超时（含 openai.APIConnectionError 全部子类）与
    408/409/429/5xx 状态错误为瞬时；其余 API 状态错误与非 openai 异常
    为内容性——后者意味着代码层面的意外，重试只是掩盖问题。
    """
    if isinstance(exc, openai.APIConnectionError):
        return _with_cause(LLMTransientError(f"LLM 服务连接失败：{exc}"), exc)
    if isinstance(exc, openai.APIStatusError):
        status_code = exc.status_code
        if status_code >= 500 or status_code in _TRANSIENT_STATUS_CODES:
            return _with_cause(LLMTransientError(f"LLM 服务瞬时错误（HTTP {status_code}）：{exc}"), exc)
        raise _with_cause(LLMContentError(f"LLM 请求被拒绝（HTTP {status_code}）：{exc}"), exc)
    raise _with_cause(LLMContentError(f"LLM 调用出现意外异常：{exc!r}"), exc)


def _with_cause(error: LLMError, cause: Exception) -> LLMError:
    error.__cause__ = cause
    return error


def is_configured(config: Settings | None = None) -> bool:
    """.env 的模型接入三项是否齐备；不齐备时应用启动可跳过 Worker。"""
    cfg = config or settings
    return bool(cfg.llm_base_url and cfg.llm_api_key and cfg.llm_model)


class PolishLLMClient:
    """openai 兼容润色客户端。``model_name`` 供 Worker 写入只读
    model_snapshot（客诉排查，spec #15 用户故事 12）；不暴露任何 key。

    :meth:`polish` 是单段落调用：输入段落文本，返回润色后的文本。
    阶段差异（润色/增强）只体现在 system_prompt，由调用方给出。
    :meth:`complete` 是通用对话调用：给定完整 messages 轮次，返回纯文本
    ——润色之外的消费方（文章预处理的分块标记、AI 规格生成）共用这一接缝，
    错误分类与退避纪律与润色同源（issue #32），测试用脚本化假客户端注入。
    """

    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        if not model:
            raise ValueError("PolishLLMClient 需要模型名")
        self.model_name = model
        self._model = model
        self._client = client

    @classmethod
    def from_settings(cls, config: Settings | None = None) -> "PolishLLMClient":
        """按 .env 配置构造（ADR-0005）；未配置时抛 ValueError
        （调用方应先用 :func:`is_configured` 判断）。

        SDK 自身的自动重试显式关闭：重试策略唯一归口于本类，退避时长
        与次数不被 SDK 暗中翻倍。
        """
        cfg = config or settings
        if not is_configured(cfg):
            raise ValueError("模型接入未配置（.env 需 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL）")
        client = AsyncOpenAI(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key, max_retries=0)
        return cls(client, cfg.llm_model)

    async def polish(self, text: str, *, system_prompt: str) -> str:
        """润色一个段落，返回模型输出的纯文本。

        瞬时错误按退避重试，重试耗尽抛 :class:`LLMTransientError`；
        内容性错误与空输出立即抛 :class:`LLMContentError`。
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ]
        return await self.complete(messages)

    async def complete(self, messages: list[dict[str, str]]) -> str:
        """通用对话调用：给定完整 messages（system/user 轮次），返回模型
        输出的纯文本。

        错误与重试纪律与 :meth:`polish` 同一套（唯一归口，不在消费方各自
        重复）：瞬时错误按退避重试，重试耗尽抛 :class:`LLMTransientError`；
        内容性错误与空输出立即抛 :class:`LLMContentError`。
        """
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                content = await self._complete_once(messages)
            except Exception as exc:
                # 内容性错误由分类器当场抛出；这里只接住瞬时错误排重试节奏
                error = classify_llm_error(exc)
                if attempt == _MAX_ATTEMPTS:
                    raise error
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
                continue
            if not content.strip():
                raise LLMContentError("模型返回空内容")
            return content
        raise AssertionError("unreachable: retry loop must return or raise")  # pragma: no cover

    async def _complete_once(self, messages: list[dict[str, str]]) -> str:
        """单次调用：成功返回文本（可能为空），失败原样抛 SDK 异常交给分类。"""
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,  # type: ignore[arg-type]
        )
        if not response.choices:
            return ""
        return response.choices[0].message.content or ""
