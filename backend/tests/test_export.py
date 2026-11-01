"""导出端点行为（HTTP 接缝）：完成态会话导出 txt / docx（issue #21）。

覆盖验收：txt 内容=最终文本按段落顺序、docx 可用 python-docx 打开且段落
内容与最终文本一致、非 completed 一律 409、归属校验 404、未认证 401、
万字级大文本导出内存受控（tracemalloc 冒烟上限）。只测外部行为。
"""

import io
import sys
import tracemalloc
from collections.abc import Callable
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models import OptimizationSession, Segment, User

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import billing, optimization  # noqa: E402

# tracemalloc 对万字级 docx 导出的冒烟上限：512MB 部署约束的宽松代理，
# 守住"渲染库引入后导出路径没有引入失控的大对象"这一回归。
_DOCX_EXPORT_MALLOC_LIMIT_BYTES = 150 * 1024 * 1024


def _grant(db_session_factory: sessionmaker, user: User, balance_fen: int) -> None:
    session = db_session_factory()
    try:
        billing.grant_initial_quota(
            session, user.id, balance_fen=balance_fen, usage_count=0, reason="发卡初始额度"
        )
        session.commit()
    finally:
        session.close()


def _make_completed_session(
    db_session_factory: sessionmaker,
    user: User,
    *,
    mode: str,
    sources: list[str],
    results_by_stage: dict[str, list[str]],
    status: str = optimization.SESSION_COMPLETED,
) -> int:
    """直接经服务层造一个（默认）完成态会话：段落结果与状态由测试写死，
    不经 LLM。返回会话 id。"""
    session = db_session_factory()
    try:
        row = optimization.create_session(session, user, text="\n".join(sources), mode=mode)
        assert row is not None
        session.commit()
        session_id = row.id
        for stage, results in results_by_stage.items():
            segments = session.execute(
                select(Segment)
                .where(Segment.session_id == session_id, Segment.stage == stage)
                .order_by(Segment.seq)
            ).scalars()
            for segment, result in zip(segments, results):
                segment.result_text = result
                segment.status = optimization.SEGMENT_COMPLETED
        row.status = status
        session.commit()
        return session_id
    finally:
        session.close()


def _docx_paragraph_texts(docx_bytes: bytes) -> list[str]:
    doc = Document(io.BytesIO(docx_bytes))
    return [p.text for p in doc.paragraphs if p.text.strip()]


# ---- txt 导出 ----


def test_txt_export_final_text_by_segment_order(
    client: TestClient,
    make_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    user = make_user(card_key="card-export-1")
    _grant(db_session_factory, user, balance_fen=5000)
    polish_results = ["润色后第一段。", "润色后第二段。", "润色后第三段。"]
    enhance_results = ["增强后第一段。", "增强后第二段。", "增强后第三段。"]
    session_id = _make_completed_session(
        db_session_factory,
        user,
        mode="paper_polish_enhance",
        sources=["原文一段", "原文二段", "原文三段"],
        results_by_stage={"polish": polish_results, "enhance": enhance_results},
    )

    response = client.get(
        f"/api/optimization/sessions/{session_id}/export.txt",
        headers={"X-Card-Key": user.card_key},
    )

    assert response.status_code == 200, response.text
    # 两阶段模式：最终文本取最后阶段（增强）的段落结果，按 seq 顺序空行分段
    assert response.text == "\n\n".join(enhance_results)
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["content-disposition"].startswith("attachment")


def test_txt_export_polish_only_mode_uses_polish_results(
    client: TestClient,
    make_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    user = make_user(card_key="card-export-2")
    _grant(db_session_factory, user, balance_fen=5000)
    polish_results = ["仅润色第一段。", "仅润色第二段。"]
    session_id = _make_completed_session(
        db_session_factory,
        user,
        mode="paper_polish",
        sources=["原文一段", "原文二段"],
        results_by_stage={"polish": polish_results},
    )

    response = client.get(
        f"/api/optimization/sessions/{session_id}/export.txt",
        headers={"X-Card-Key": user.card_key},
    )

    assert response.status_code == 200, response.text
    assert response.text == "\n\n".join(polish_results)


# ---- docx 导出 ----


def test_docx_export_paragraphs_match_final_text(
    client: TestClient,
    make_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    user = make_user(card_key="card-export-3")
    _grant(db_session_factory, user, balance_fen=5000)
    final_segments = ["增强后第一段内容。", "增强后第二段内容。", "增强后第三段内容。"]
    session_id = _make_completed_session(
        db_session_factory,
        user,
        mode="paper_polish_enhance",
        sources=["原文一段", "原文二段", "原文三段"],
        results_by_stage={
            "polish": ["润色后第一段内容。", "润色后第二段内容。", "润色后第三段内容。"],
            "enhance": final_segments,
        },
    )

    response = client.get(
        f"/api/optimization/sessions/{session_id}/export.docx",
        headers={"X-Card-Key": user.card_key},
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    # python-docx 回读：除目录占位两行外，段落内容与顺序=最终文本逐段一段
    assert _docx_paragraph_texts(response.content) == [
        "目 录",
        "（在 Word 中右键目录 → 更新域）",
        *final_segments,
    ]


def test_docx_export_polish_only_mode(
    client: TestClient,
    make_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    user = make_user(card_key="card-export-4")
    _grant(db_session_factory, user, balance_fen=5000)
    polish_results = ["仅润色甲段。", "仅润色乙段。"]
    session_id = _make_completed_session(
        db_session_factory,
        user,
        mode="paper_polish",
        sources=["原文甲段", "原文乙段"],
        results_by_stage={"polish": polish_results},
    )

    response = client.get(
        f"/api/optimization/sessions/{session_id}/export.docx",
        headers={"X-Card-Key": user.card_key},
    )

    assert response.status_code == 200, response.text
    assert _docx_paragraph_texts(response.content) == [
        "目 录",
        "（在 Word 中右键目录 → 更新域）",
        *polish_results,
    ]


# ---- 只读守卫与归属 ----


@pytest.mark.parametrize("status_value", ["queued", "running", "failed", "cancelled"])
def test_export_rejects_non_completed_session_with_409(
    client: TestClient,
    make_user: Callable[..., User],
    db_session_factory: sessionmaker,
    status_value: str,
) -> None:
    user = make_user(card_key=f"card-export-{status_value}")
    _grant(db_session_factory, user, balance_fen=5000)
    session_id = _make_completed_session(
        db_session_factory,
        user,
        mode="paper_polish",
        sources=["原文一段"],
        results_by_stage={"polish": ["润色后第一段。"]},
        status=status_value,
    )

    for path in ("export.txt", "export.docx"):
        response = client.get(
            f"/api/optimization/sessions/{session_id}/{path}",
            headers={"X-Card-Key": user.card_key},
        )
        assert response.status_code == 409, (path, response.status_code)
        assert "尚未完成" in response.json()["detail"]


def test_export_hides_other_users_sessions_with_404(
    client: TestClient,
    make_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    owner = make_user(card_key="card-export-owner")
    _grant(db_session_factory, owner, balance_fen=5000)
    other = make_user(card_key="card-export-other")
    session_id = _make_completed_session(
        db_session_factory,
        owner,
        mode="paper_polish",
        sources=["原文一段"],
        results_by_stage={"polish": ["润色后第一段。"]},
    )

    for path in ("export.txt", "export.docx"):
        response = client.get(
            f"/api/optimization/sessions/{session_id}/{path}",
            headers={"X-Card-Key": other.card_key},
        )
        assert response.status_code == 404, (path, response.status_code)


def test_export_requires_card_key(
    client: TestClient,
    make_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    owner = make_user(card_key="card-export-noauth")
    _grant(db_session_factory, owner, balance_fen=5000)
    session_id = _make_completed_session(
        db_session_factory,
        owner,
        mode="paper_polish",
        sources=["原文一段"],
        results_by_stage={"polish": ["润色后第一段。"]},
    )

    for path in ("export.txt", "export.docx"):
        response = client.get(f"/api/optimization/sessions/{session_id}/{path}")
        assert response.status_code == 401, (path, response.status_code)


def test_export_missing_session_404(
    client: TestClient, make_user: Callable[..., User]
) -> None:
    user = make_user(card_key="card-export-missing")
    response = client.get(
        "/api/optimization/sessions/999999/export.txt",
        headers={"X-Card-Key": user.card_key},
    )
    assert response.status_code == 404


# ---- 大文本内存冒烟 ----


def test_docx_export_ten_thousand_chars_memory_bounded(
    client: TestClient,
    make_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    user = make_user(card_key="card-export-large")
    _grant(db_session_factory, user, balance_fen=500_000)
    # 12 段 × 1000 字 ≈ 1.2 万字（万字级）
    final_segments = [f"第{i}段研究成果正文。" + "文" * 984 for i in range(12)]
    session_id = _make_completed_session(
        db_session_factory,
        user,
        mode="paper_polish",
        sources=[f"第{i}段原始文本。" + "原" * 984 for i in range(12)],
        results_by_stage={"polish": final_segments},
    )

    tracemalloc.start()
    response = client.get(
        f"/api/optimization/sessions/{session_id}/export.docx",
        headers={"X-Card-Key": user.card_key},
    )
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert response.status_code == 200, response.text
    assert peak < _DOCX_EXPORT_MALLOC_LIMIT_BYTES, f"导出内存峰值 {peak} 字节超限"
    paragraphs = _docx_paragraph_texts(response.content)
    assert len(paragraphs) == 14  # 目录占位两行 + 12 段
