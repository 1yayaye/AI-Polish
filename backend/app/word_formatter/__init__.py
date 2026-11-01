"""word_formatter 渲染核心：自旧版（main 分支）整体黑盒移植的纯函数库（ADR-0004）。

移植边界（T6，spec #15 用户故事 20）：仅引入 Markdown/纯文本 → AST →
StyleSpec → OOXML 的编译管线（compiler + renderer 及其依赖
ast_generator / spec_generator / template_generator / validator / fixer 与
models、utils 层），内部代码与原实现逐字节一致（git show main: 对比为零 diff）；
旧版的 API 路由、JobManager、认证与配置耦合不在移植范围（API 路由与任务
管理归 M4，届时以新架构重写外围）。

调用方（docx 导出等）只需要 :func:`compile_document`：输入文本、可选
:class:`CompileOptions`，得到 :class:`CompileResult`（success + docx 字节
流）。同步阻塞函数——在 asyncio 中使用时走 ``asyncio.to_thread``（见
app.export 的单并发编译约定）。
"""

from .models.ast import DocumentAST
from .models.stylespec import StyleSpec
from .models.validation import ValidationReport
from .services.compiler import (
    CompileOptions,
    CompilePhase,
    CompileProgress,
    CompileResult,
    InputFormat,
    compile_document,
    detect_input_format,
)
from .services.renderer import RenderOptions, render_docx

__all__ = [
    "DocumentAST",
    "StyleSpec",
    "ValidationReport",
    "CompileOptions",
    "CompilePhase",
    "CompileProgress",
    "CompileResult",
    "InputFormat",
    "compile_document",
    "detect_input_format",
    "RenderOptions",
    "render_docx",
]
