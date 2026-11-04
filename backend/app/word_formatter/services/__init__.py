"""word_formatter 服务层（适配层）。

黑盒移植只带入编译管线的七个服务（compiler/renderer/validator/fixer/
ast_generator/spec_generator/template_generator）；旧版的 job_manager、
preprocessor、format_checker 属于 M4 整体移植范围，不在此导入。
"""

from .ast_generator import (
    VALID_MARKER_TYPES,
    identify_paragraph_type,
    parse_marked_text_to_ast,
    parse_markdown_to_ast,
    parse_plaintext_heuristic,
)
from .compiler import (
    CompileOptions,
    CompilePhase,
    CompileProgress,
    CompileResult,
    InputFormat,
    compile_document,
    detect_input_format,
)
from .fixer import apply_patch, build_patch_from_report, fix_docx
from .renderer import RenderOptions, render_docx
from .spec_generator import (
    ai_generate_spec,
    builtin_specs,
    build_generic_spec,
    export_spec_to_json,
    get_spec_schema,
    validate_custom_spec,
)
from .template_generator import generate_reference_docx, patch_reference_docx
from .validator import validate_docx

__all__ = [
    # AST Generator
    "parse_markdown_to_ast",
    "parse_plaintext_heuristic",
    "parse_marked_text_to_ast",
    "identify_paragraph_type",
    "VALID_MARKER_TYPES",
    # Spec Generator
    "ai_generate_spec",
    "build_generic_spec",
    "builtin_specs",
    "validate_custom_spec",
    "export_spec_to_json",
    "get_spec_schema",
    # Template Generator
    "generate_reference_docx",
    "patch_reference_docx",
    # Renderer
    "render_docx",
    "RenderOptions",
    # Validator
    "validate_docx",
    # Fixer
    "fix_docx",
    "build_patch_from_report",
    "apply_patch",
    # Compiler
    "compile_document",
    "CompileOptions",
    "CompileResult",
    "CompileProgress",
    "CompilePhase",
    "InputFormat",
    "detect_input_format",
]
