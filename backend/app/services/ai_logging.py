"""Safe debug logging helpers for AI requests and responses."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.config import settings


SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)(\s*[:=]\s*)([^\s,;]+)"),
]


def is_ai_debug_logging_enabled() -> bool:
    """Return whether verbose AI debug logging is enabled."""
    return bool(getattr(settings, "AI_DEBUG_LOGGING", False))


def redact_sensitive(value: Any) -> str:
    """Redact common key/token/password patterns from a value."""
    text = "" if value is None else str(value)
    text = SECRET_PATTERNS[0].sub("sk-****", text)
    text = SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)}{m.group(2)}****", text)
    return text


def truncate_preview(value: Any, max_chars: int = 120) -> str:
    """Return a redacted, single-line preview capped at max_chars."""
    text = redact_sensitive(value).replace("\r", " ").replace("\n", " ")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def summarize_messages(messages: List[Dict[str, str]], max_preview_chars: int = 120) -> List[Dict[str, Any]]:
    """Summarize messages without logging full prompt/user content."""
    summary: List[Dict[str, Any]] = []
    for message in messages:
        content = message.get("content") or ""
        summary.append(
            {
                "role": message.get("role", "unknown"),
                "content_length": len(content),
                "preview": truncate_preview(content, max_preview_chars),
            }
        )
    return summary


def log_ai_request(
    label: str,
    model: str,
    messages: List[Dict[str, str]],
    *,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    reasoning_effort: Optional[str] = None,
    stream: bool = False,
) -> None:
    """Print a safe AI request summary for opt-in debug mode."""
    print("\n" + "=" * 80, flush=True)
    print(f"[{label}] Request", flush=True)
    print(f"[{label}] Model: {truncate_preview(model, 80)}", flush=True)
    print(f"[{label}] Stream: {stream}", flush=True)
    if reasoning_effort and reasoning_effort != "none":
        print(f"[{label}] Reasoning Effort: {truncate_preview(reasoning_effort, 40)}", flush=True)
    else:
        print(f"[{label}] Temperature: {temperature}", flush=True)
    print(f"[{label}] Max Tokens: {max_tokens}", flush=True)
    print(f"[{label}] Messages Summary: {summarize_messages(messages)}", flush=True)
    print("=" * 80 + "\n", flush=True)


def log_ai_response(
    label: str,
    content: str,
    *,
    response_id: Optional[str] = None,
    response_model: Optional[str] = None,
    usage: Optional[Any] = None,
) -> None:
    """Print a safe AI response summary for opt-in debug mode."""
    print("\n" + "=" * 80, flush=True)
    print(f"[{label}] Response", flush=True)
    if response_id:
        print(f"[{label}] ID: {truncate_preview(response_id, 80)}", flush=True)
    if response_model:
        print(f"[{label}] Model: {truncate_preview(response_model, 80)}", flush=True)
    if usage:
        print(f"[{label}] Prompt Tokens: {getattr(usage, 'prompt_tokens', None)}", flush=True)
        print(f"[{label}] Completion Tokens: {getattr(usage, 'completion_tokens', None)}", flush=True)
        print(f"[{label}] Total Tokens: {getattr(usage, 'total_tokens', None)}", flush=True)
    print(f"[{label}] Content Length: {len(content or '')}", flush=True)
    print(f"[{label}] Preview: {truncate_preview(content, 120)}", flush=True)
    print("=" * 80 + "\n", flush=True)


def log_ai_error(label: str, error: Exception) -> None:
    """Print a safe AI error summary for opt-in debug mode."""
    print("\n" + "=" * 80, flush=True)
    print(f"[{label}] Error Type: {type(error).__name__}", flush=True)
    print(f"[{label}] Error: {truncate_preview(error, 240)}", flush=True)
    print("=" * 80 + "\n", flush=True)
