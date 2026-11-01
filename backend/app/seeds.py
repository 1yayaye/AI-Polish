"""系统预设提示词的代码种子：调优资产固化进代码，空库首次启动时注入。

方案 A（issue #23 裁决，issue #30 落地）后，custom_prompts 的 is_system 行
是系统提示词唯一事实源（运行时消费只读库，app.prompts），本模块的
``app/seed_data/system_prompts.json``（T5 迁移 ``--export-seed`` 从旧库导出）
降级为**空库首次注入的种子**：应用启动（app.main.lifespan）与旧库迁移脚本
（backend/scripts/migrate_old_db.py）都会调用 :func:`seed_system_prompts`。
注入是幂等的：以 (is_system, user_id IS NULL, stage, name) 判重，已存在
（含内容被后续运营在后台改过）的行不会被覆盖——种子的职责是"从无到有"，
不是"保持一致"，库内行的后续演化只发生在数据库里。

独立注入入口：``python -m app.seeds``（对 .env 指向的库执行）。
"""

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CustomPrompt

SEED_FILE = Path(__file__).parent / "seed_data" / "system_prompts.json"


def load_system_prompts(path: Path | None = None) -> list[dict]:
    """读取种子文件，返回可交给 :func:`seed_system_prompts` 的提示词字典列表。

    文件缺失或结构不对当场抛错：种子是空库初始化的唯一来源，静默回退到
    空列表等于悄悄丢失运营商资产。
    """
    with (path or SEED_FILE).open(encoding="utf-8") as f:
        data = json.load(f)
    prompts = data.get("prompts")
    if not isinstance(prompts, list):
        raise ValueError(f"种子文件结构不对（缺 prompts 数组）：{path or SEED_FILE}")
    return prompts


def seed_system_prompts(session: Session, prompts: list[dict] | None = None) -> int:
    """把系统预设提示词注入库中（幂等），返回本次新插入的条数。

    在调用方的事务内写入：迁移脚本由此保证种子与数据迁移同提交同回滚；
    独立使用（CLI）时由 main() 提交。判重键为
    (is_system=True, user_id IS NULL, stage, name)，不按内容比较——
    运营商在库内改过的正文不会被种子回滚覆盖。
    """
    inserted = 0
    for item in prompts if prompts is not None else load_system_prompts():
        exists = session.execute(
            select(CustomPrompt.id).where(
                CustomPrompt.is_system.is_(True),
                CustomPrompt.user_id.is_(None),
                CustomPrompt.stage == item["stage"],
                CustomPrompt.name == item["name"],
            )
        ).first()
        if exists is not None:
            continue
        session.add(
            CustomPrompt(
                user_id=None,
                name=item["name"],
                stage=item["stage"],
                content=item["content"],
                is_default=item.get("is_default", True),
                is_system=True,
                is_active=item.get("is_active", True),
            )
        )
        inserted += 1
    return inserted


def main() -> None:
    """CLI 入口：向 .env 指向的库注入系统预设提示词（幂等，可重复执行）。"""
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        inserted = seed_system_prompts(session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    print(f"系统预设提示词注入完成：本次新插入 {inserted} 条")


if __name__ == "__main__":
    main()
