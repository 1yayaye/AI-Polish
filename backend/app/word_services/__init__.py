"""Word Formatter 服务层：自旧版（main 分支）黑盒随迁的三个服务文件（issue #32）。

随迁边界与 ADR-0004 同纪律：内部代码与原实现逐字节一致（git show main: 对比
为零 diff），仅适配新架构边界——渲染核心留在 :mod:`app.word_formatter` 纯函数
库（零改动），旧版的 .doc→.docx 转换（LibreOffice 死代码）不迁。

调用方（预处理路由、格式检查路由、后续 Worker）只需要本包公开的三个入口：

- :class:`ArticlePreprocessor`：文章预处理（确定性分段 → 分块 AI 标记 →
  组装 → 一致性校验）。构造时注入具备 ``complete(messages)`` 能力的 LLM
  客户端（生产接 :class:`app.llm.PolishLLMClient`，测试接脚本化假客户端）。
- :func:`check_format`：格式检查（纯规则、同步、宽松/严格两档）。
- :func:`extract_text_from_docx`：.docx 提取纯文本（上传文件转纯文本用）。
"""

from .docx_text import extract_text_from_docx
from .format_checker import (
    CheckMode,
    FormatCheckResult,
    FormatChecker,
    FormatIssue,
    IssueSeverity,
    IssueType,
    check_format,
)
from .preprocessor import (
    ArticlePreprocessor,
    PreprocessConfig,
    PreprocessPhase,
    PreprocessProgress,
    PreprocessResult,
)

__all__ = [
    "ArticlePreprocessor",
    "PreprocessConfig",
    "PreprocessPhase",
    "PreprocessProgress",
    "PreprocessResult",
    "CheckMode",
    "FormatCheckResult",
    "FormatChecker",
    "FormatIssue",
    "IssueSeverity",
    "IssueType",
    "check_format",
    "extract_text_from_docx",
]
