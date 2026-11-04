"""文章预处理器（issue #32，黑盒随迁自旧版）。

只从公开接口（app.word_services.ArticlePreprocessor）驱动：分段 → 分块 AI
标记 → 组装 → 一致性校验全走 preprocess()，LLM 依赖用脚本化假客户端注入
（照现有流水线测试先例，不 mock 内部函数）。进度经回调收集断言。
"""

import asyncio
import json

from app.llm import LLMContentError, LLMTransientError
from app.word_services import ArticlePreprocessor, PreprocessConfig, PreprocessPhase

SAMPLE_TEXT = "第一章 绪论\n\n这是正文段落。\n\n第二章 方法\n\n正文二。"


class ScriptedMarkingLLM:
    """complete(messages) 假客户端：按脚本顺序返回 JSON 文本或抛异常，记录调用。"""

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return str(outcome)


def _marking(indices_types: list[tuple[int, str]], *, fenced: bool = False) -> str:
    payload = json.dumps([{"index": i, "type": t} for i, t in indices_types], ensure_ascii=False)
    return f"```json\n{payload}\n```" if fenced else payload


def _run(llm: object, text: str = SAMPLE_TEXT, **config: object) -> tuple[object, list[object]]:
    events: list[object] = []
    preprocessor = ArticlePreprocessor(llm, PreprocessConfig(**config))  # type: ignore[arg-type]
    result = asyncio.run(preprocessor.preprocess(text, progress_callback=events.append))
    return result, events


def test_preprocess_marks_chunks_assembles_and_passes_integrity() -> None:
    llm = ScriptedMarkingLLM(
        [
            _marking([(0, "heading_1"), (1, "body")], fenced=True),
            _marking([(2, "heading_2"), (3, "body")]),
        ]
    )

    result, _ = _run(llm, chunk_paragraphs=2)  # 4 段 → 两块，覆盖多块组装

    assert result.success is True
    assert result.integrity_check_passed is True
    assert result.original_hash != ""
    assert result.type_statistics == {"heading_1": 1, "body": 2, "heading_2": 1}
    assert result.paragraphs[0].paragraph_type == "heading_1"
    assert result.paragraphs[0].is_rule_identified is False
    assert result.warnings == []
    # 标记组装：每段前插入 wf:type 标记，去除标记后与原文一致
    assert result.marked_text == (
        "<!-- wf:type=heading_1 -->\n第一章 绪论\n\n"
        "<!-- wf:type=body -->\n这是正文段落。\n\n"
        "<!-- wf:type=heading_2 -->\n第二章 方法\n\n"
        "<!-- wf:type=body -->\n正文二。"
    )
    assert len(llm.calls) == 2
    # 分块请求带 system 约束与全文段落上下文
    assert llm.calls[0][0]["role"] == "system"


def test_preprocess_reports_phase_and_chunk_progress() -> None:
    llm = ScriptedMarkingLLM(
        [_marking([(0, "heading_1"), (1, "body")]), _marking([(2, "body"), (3, "body")])]
    )

    _, events = _run(llm, chunk_paragraphs=2)

    phases = [e.phase for e in events]
    assert phases[0] is PreprocessPhase.SPLITTING
    assert phases[-1] is PreprocessPhase.COMPLETED
    assert PreprocessPhase.MARKING in phases and PreprocessPhase.VALIDATING in phases
    marking = [e for e in events if e.phase is PreprocessPhase.MARKING]
    assert [(e.current_chunk, e.total_chunks) for e in marking] == [(0, 2), (1, 2), (2, 2)]
    assert "分块 1/2" in marking[1].message
    # 进度携带已处理段数：块事件在处理前发出（带到当时为止的完成数）
    assert [e.processed_paragraphs for e in marking] == [0, 0, 2]
    assert events[-1].processed_paragraphs == 4  # 终态覆盖全部段落


def test_preprocess_llm_content_failure_falls_back_to_rules_with_warning() -> None:
    llm = ScriptedMarkingLLM([LLMContentError("配额耗尽")])

    result, _ = _run(llm)

    assert result.success is True  # 预处理不因标记失败整体失败：降级为规则识别
    assert any("AI 标记失败" in w for w in result.warnings)
    assert result.integrity_check_passed is True
    # 规则识别兜底：中文章标题与正文各归其位
    assert result.paragraphs[0].paragraph_type == "heading_1"
    assert result.paragraphs[1].paragraph_type == "body"
    assert all(p.is_rule_identified for p in result.paragraphs)
    assert len(llm.calls) == 1  # 内容性失败不重试


def test_preprocess_llm_transient_failure_falls_back_to_rules_with_warning() -> None:
    llm = ScriptedMarkingLLM([LLMTransientError("重试耗尽")])

    result, _ = _run(llm)

    assert result.success is True
    assert any("AI 标记失败" in w for w in result.warnings)
    assert all(p.is_rule_identified for p in result.paragraphs)


def test_preprocess_invalid_llm_json_is_retried_then_falls_back() -> None:
    llm = ScriptedMarkingLLM(["这不是 JSON", "还不是 JSON", "仍然不是 JSON"])

    result, _ = _run(llm)  # 默认 max_retries=2：同一分块至多 3 次尝试

    assert result.success is True
    assert any("AI 标记失败" in w for w in result.warnings)
    assert len(llm.calls) == 3  # 响应不可解析按内容性问题在块内重试后放弃


def test_preprocess_empty_text_fails_without_calling_llm() -> None:
    llm = ScriptedMarkingLLM([])

    result, events = _run(llm, text="   \n\n  ")

    assert result.success is False
    assert result.error == "文章内容为空或无法分段"
    assert llm.calls == []
    # 空文本走早退路径：只发出首个分段进度，不进入标记阶段
    assert [e.phase for e in events] == [PreprocessPhase.SPLITTING]
