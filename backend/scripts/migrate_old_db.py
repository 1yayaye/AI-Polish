"""旧库 → 新库的一次性数据迁移（issue #8，T5）。

## 迁什么

- **users**：卡密 + 双轨额度（旧 ``workspace_balance_cents`` → ``balance_fen``；
  旧 ``usage_count`` → ``usage_used``）。旧 ``is_active``/``access_link`` 在新
  架构中不存在（无停用概念、卡密即身份），不迁。
- **billing_transactions → ledger_entries**：逐行搬入。类型映射
  workspace_precharge/workspace_refund/admin_adjustment → charge/refund/adjust，
  未知类型当场中止（宁可不迁，不可猜错钱）。旧 ``optimization_session_id``
  → 无外键 ``task_id``（加 :data:`LEGACY_TASK_ID_OFFSET` 偏移），引用已不存在的
  旧会话也不断裂（rewrite-plan §5）。
- **custom_prompts**：仅用户自定义行（is_system=False）。
- **saved_specs**：全量。
- **is_system 提示词**：不作为数据行迁移——正文唯一来源是代码种子
  （``app/seed_data/system_prompts.json``），迁移在同一事务内经
  ``app.seeds.seed_system_prompts`` 注入。``--export-seed`` 从旧库重新导出该
  文件（运营商在旧系统里调优过提示词时，T6 迁移前先导出并提交）。

## 关键决策

- **单事务，硬校验，不通过即整体回滚**：全部写入在新库一个事务内完成，
  随后逐用户校验 ``SUM(账本) == 余额``；任何差异抛 :class:`MigrationError`
  带差异明细，事务回滚，新库保持迁移前状态。
- **直接写 users 资金字段是全系统唯一例外**：迁移搬的是"初始状态 + 全部
  历史流水"的整体，不走计费服务（再插一条合成 issue 行会伪造历史）；
  SUM 硬校验就是这条例外路径的守卫。
- **task_id 偏移**：新任务自增 id 从 1 开始，与旧会话 id 天然重叠；旧 refund
  行若保留小 task_id，会命中部分唯一索引 ``ux_ledger_refund_once_per_task``，
  使恰好同号的新任务永远无法退款（静默丢钱）。偏移 10 亿让两个 id 空间
  不相交。
- **幂等**：状态记在新库 ``migration_state`` 表（旧库快照指纹 + 迁移时间，
  本脚本私有，应用代码不读）。重跑时指纹一致则跳过写入（退出码 0，仍做
  一次对账复核）；指纹不一致说明换了一份旧库快照，中止由人裁决。失败
  回滚不留痕迹，重跑即从头开始。

## 用法

::

    # 1.（如旧系统里调优过系统提示词）先导出种子并提交
    python -m scripts.migrate_old_db 旧库.db --export-seed
    # 2. 对新库执行迁移（新库需已 alembic upgrade head）
    python -m scripts.migrate_old_db 旧库.db

对生产库副本执行与对账报告留存属 T6，由运营商完成。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import MetaData, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import CustomPrompt, LedgerEntry, SavedSpec, User
from app.seeds import SEED_FILE, seed_system_prompts

# 旧会话 id → 账本 task_id 的统一偏移，见模块级"关键决策"。
LEGACY_TASK_ID_OFFSET = 1_000_000_000

# 旧账本交易类型 → 新账本 kind（kind 取值由 app.billing 拥有）
_KIND_BY_LEGACY_TYPE = {
    "workspace_precharge": "charge",
    "workspace_refund": "refund",
    "admin_adjustment": "adjust",
}

_MARKER_KEY = "legacy_migration_v1"
_REASON_MAX = 200  # ledger_entries.reason 的列宽
_MISSING_REASON = "迁移自旧账本（{legacy_type}）"

_LEGACY_TABLES = ("users", "billing_transactions", "custom_prompts", "saved_specs")


class MigrationError(Exception):
    """迁移失败（旧库结构/数据不合法、快照不符、对账不过），携带给运营商的中文明细。"""


def _utcnow() -> datetime:
    """旧库时间戳是 naive UTC（旧代码用 datetime.utcnow），回退值保持同一口径。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class MigrationReport:
    """一次迁移运行的结果，CLI 据此打印对账报告。"""

    users: int = 0
    ledger_entries: int = 0
    custom_prompts: int = 0
    saved_specs: int = 0
    system_prompts_seeded: int = 0
    # 每用户 (user_id, 卡密脱敏, SUM(账本), 余额)；迁移后全部相等
    reconciliation: list[tuple[int, str, int, int]] = field(default_factory=list)
    # True 表示此前已成功迁移过同一快照（幂等跳过），本次未写入任何数据
    already_migrated: bool = False
    source_fingerprint: str = ""


def _mask_card_key(card_key: str) -> str:
    if len(card_key) <= 8:
        return card_key[:2] + "****"
    return card_key[:4] + "****" + card_key[-4:]


def _legacy_tables(old_engine: Engine) -> dict:
    """反射旧库四张源表；缺表当场报错，不带病续迁。"""
    metadata = MetaData()
    metadata.reflect(bind=old_engine)
    tables = {}
    for name in _LEGACY_TABLES:
        table = metadata.tables.get(name)
        if table is None:
            raise MigrationError(f"旧库缺少表 {name}，不是预期的旧版数据库")
        tables[name] = table
    return tables


def _fingerprint(old_engine: Engine, tables: dict) -> str:
    """旧库快照指纹：各表行数 + 最大 id，足够识别"是同一份快照"这件事。"""
    with old_engine.connect() as conn:
        parts = []
        for name in _LEGACY_TABLES:
            table = tables[name]
            count = conn.execute(select(func.count()).select_from(table)).scalar_one()
            max_id = conn.execute(select(func.max(table.c.id))).scalar_one()
            parts.append(f"{name}={count}/{max_id}")
    return ";".join(parts)


def _read_legacy(old_engine: Engine, tables: dict) -> dict:
    """读出旧库全部待迁数据。迁移库量小，整读进内存：写事务期间不碰旧库，
    读到的就是一份稳定快照。"""
    users_t = tables["users"]
    tx_t = tables["billing_transactions"]
    prompts_t = tables["custom_prompts"]
    specs_t = tables["saved_specs"]

    with old_engine.connect() as conn:
        users = conn.execute(
            select(
                users_t.c.id,
                users_t.c.card_key,
                func.coalesce(users_t.c.workspace_balance_cents, 0).label("balance_fen"),
                func.coalesce(users_t.c.usage_limit, 0).label("usage_limit"),
                func.coalesce(users_t.c.usage_count, 0).label("usage_used"),
            ).order_by(users_t.c.id)
        ).mappings().all()
        transactions = conn.execute(
            select(
                tx_t.c.id,
                tx_t.c.user_id,
                tx_t.c.optimization_session_id,
                tx_t.c.transaction_type,
                tx_t.c.amount_cents,
                tx_t.c.balance_after_cents,
                tx_t.c.reason,
                tx_t.c.created_at,
            ).order_by(tx_t.c.id)
        ).mappings().all()
        # is_system 行固化为代码种子，不作为数据迁移（见模块级"迁什么"）；
        # 旧库布尔列历经多次手工加列，一律 coalesce 掉 NULL 再比较
        prompts = conn.execute(
            select(
                prompts_t.c.id,
                prompts_t.c.user_id,
                prompts_t.c.name,
                prompts_t.c.stage,
                prompts_t.c.content,
                func.coalesce(prompts_t.c.is_default, False).label("is_default"),
                func.coalesce(prompts_t.c.is_active, True).label("is_active"),
                prompts_t.c.created_at,
                prompts_t.c.updated_at,
            )
            .where(func.coalesce(prompts_t.c.is_system, False) != True)  # noqa: E712
            .order_by(prompts_t.c.id)
        ).mappings().all()
        specs = conn.execute(
            select(
                specs_t.c.id,
                specs_t.c.user_id,
                specs_t.c.name,
                specs_t.c.description,
                specs_t.c.spec_json,
                specs_t.c.created_at,
                specs_t.c.updated_at,
            ).order_by(specs_t.c.id)
        ).mappings().all()

    return {
        "users": users,
        "transactions": transactions,
        "prompts": prompts,
        "specs": specs,
    }


def _validate_snapshot(data: dict) -> None:
    """写库前就能判定的坏数据在这里挡下，错误信息指到具体旧行。"""
    unknown = sorted({row["transaction_type"] for row in data["transactions"]} - set(_KIND_BY_LEGACY_TYPE))
    if unknown:
        raise MigrationError(f"旧账本出现未知交易类型 {unknown}，映射需人工确认后再迁移")

    # 新账本的部分唯一索引要求每个 task 至多一笔 refund；旧库同一会话多笔
    # 退款只能说明旧数据本身有疑问，宁可不迁
    refund_counts: dict[int, int] = {}
    for row in data["transactions"]:
        if row["transaction_type"] == "workspace_refund" and row["optimization_session_id"] is not None:
            session_id = row["optimization_session_id"]
            refund_counts[session_id] = refund_counts.get(session_id, 0) + 1
    duplicated = sorted(session_id for session_id, n in refund_counts.items() if n > 1)
    if duplicated:
        raise MigrationError(
            f"旧账本中旧会话 {duplicated} 各有多笔退款，与新账本『每 task 至多一笔"
            f" refund』的约束冲突，请先在旧库核实这些行"
        )

    known_user_ids = {row["id"] for row in data["users"]}
    for row in data["transactions"]:
        if row["user_id"] not in known_user_ids:
            raise MigrationError(f"旧账本第 {row['id']} 行指向不存在的用户 {row['user_id']}")

    # custom_prompts.user_id 可空（全局公共模板，None 合法）；saved_specs.user_id
    # 非空列。悬空引用一旦漏进写库阶段就是裸 AttributeError/KeyError，这里统一挡下
    for table_name, rows in (("custom_prompts", data["prompts"]), ("saved_specs", data["specs"])):
        for row in rows:
            if row["user_id"] is not None and row["user_id"] not in known_user_ids:
                raise MigrationError(
                    f"旧库 {table_name} 第 {row['id']} 行（{row['name']}）"
                    f"指向不存在的用户 {row['user_id']}"
                )


def _insert_ledger_entries(session: Session, transactions, user_id_map: dict[int, User]) -> int:
    for row in transactions:
        task_id = row["optimization_session_id"]
        reason = (row["reason"] or "").strip() or _MISSING_REASON.format(
            legacy_type=row["transaction_type"]
        )
        session.add(
            LedgerEntry(
                user_id=user_id_map[row["user_id"]].id,
                task_id=(task_id + LEGACY_TASK_ID_OFFSET) if task_id is not None else None,
                kind=_KIND_BY_LEGACY_TYPE[row["transaction_type"]],
                amount_fen=row["amount_cents"],
                balance_after_fen=row["balance_after_cents"],
                usage_after=None,  # 旧账本只有余额轨流水，次数轨无历史可迁
                reason=reason[:_REASON_MAX],
                created_at=row["created_at"] or _utcnow(),
            )
        )
    return len(transactions)


def _reconcile(session: Session) -> list[tuple[int, str, int, int]]:
    """逐用户对账 SUM(账本) == 余额；有差异抛 :class:`MigrationError`（含明细）。"""
    sums = dict(
        session.execute(
            select(LedgerEntry.user_id, func.sum(LedgerEntry.amount_fen)).group_by(
                LedgerEntry.user_id
            )
        ).all()
    )
    rows: list[tuple[int, str, int, int]] = []
    mismatches: list[tuple[int, str, int, int]] = []
    for user in session.execute(select(User).order_by(User.id)).scalars():
        ledger_sum = sums.get(user.id) or 0
        row = (user.id, _mask_card_key(user.card_key), ledger_sum, user.balance_fen)
        rows.append(row)
        if ledger_sum != user.balance_fen:
            mismatches.append(row)
    if mismatches:
        detail = "\n".join(
            f"  user_id={uid} 卡密={card_key} SUM(账本)={ledger_sum} 余额={balance}"
            f" 差额={ledger_sum - balance}"
            for uid, card_key, ledger_sum, balance in mismatches
        )
        raise MigrationError(
            f"硬校验失败：{len(mismatches)} 个用户 SUM(账本) != 余额，本次迁移已回滚。"
            f"差异明细：\n{detail}"
        )
    return rows


def migrate_legacy_db(old_engine: Engine, new_engine: Engine) -> MigrationReport:
    """把旧库数据整体迁入新库：单事务、幂等、迁移后硬对账（见模块级文档）。

    新库必须已建表（alembic upgrade head）。抛 :class:`MigrationError` 表示
    本次没有写入任何数据（失败已回滚，或快照不符被拒）。
    """
    tables = _legacy_tables(old_engine)
    data = _read_legacy(old_engine, tables)
    _validate_snapshot(data)
    fingerprint = _fingerprint(old_engine, tables)

    with new_engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS migration_state ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL,"
            "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        marker = conn.execute(
            text("SELECT value FROM migration_state WHERE key = :key"), {"key": _MARKER_KEY}
        ).first()
        if marker is not None:
            previous = json.loads(marker[0])
            if previous.get("fingerprint") != fingerprint:
                raise MigrationError(
                    f"该新库此前迁移过另一份旧库快照（{previous.get('fingerprint')}），"
                    f"本次快照为 {fingerprint}。重复迁移只应发生在重建的空新库上，"
                    "请人工确认后再执行"
                )
            # 幂等跳过：不写入，但顺手做一次对账复核（账本可能被后续运营改动）
            session = Session(bind=conn, autoflush=False, expire_on_commit=False)
            try:
                reconciliation = _reconcile(session)
            finally:
                session.close()
            return MigrationReport(
                already_migrated=True, source_fingerprint=fingerprint,
                reconciliation=reconciliation,
            )

        session = Session(bind=conn, autoflush=False, expire_on_commit=False)
        try:
            user_id_map: dict[int, User] = {}
            for row in data["users"]:
                user = User(
                    card_key=row["card_key"],
                    balance_fen=row["balance_fen"],
                    usage_limit=row["usage_limit"],
                    usage_used=row["usage_used"],
                )
                session.add(user)
                user_id_map[row["id"]] = user
            session.flush()  # 取得新 user id，账本/提示词/规格才能挂上去

            report = MigrationReport(source_fingerprint=fingerprint)
            report.users = len(user_id_map)
            report.ledger_entries = _insert_ledger_entries(
                session, data["transactions"], user_id_map
            )
            for row in data["prompts"]:
                session.add(
                    CustomPrompt(
                        user_id=user_id_map[row["user_id"]].id if row["user_id"] is not None else None,
                        name=row["name"],
                        stage=row["stage"],
                        content=row["content"],
                        is_default=bool(row["is_default"]),
                        is_system=False,
                        is_active=bool(row["is_active"]),
                        created_at=row["created_at"] or _utcnow(),
                        updated_at=row["updated_at"] or _utcnow(),
                    )
                )
            report.custom_prompts = len(data["prompts"])
            for row in data["specs"]:
                session.add(
                    SavedSpec(
                        user_id=user_id_map[row["user_id"]].id,
                        name=row["name"],
                        description=row["description"],
                        spec_json=row["spec_json"],
                        created_at=row["created_at"] or _utcnow(),
                        updated_at=row["updated_at"] or _utcnow(),
                    )
                )
            report.saved_specs = len(data["specs"])
            report.system_prompts_seeded = seed_system_prompts(session)
            session.flush()

            report.reconciliation = _reconcile(session)
            session.execute(
                text("INSERT INTO migration_state (key, value) VALUES (:key, :value)"),
                {
                    "key": _MARKER_KEY,
                    "value": json.dumps(
                        {"fingerprint": fingerprint, "migrated_at": _utcnow().isoformat()},
                        ensure_ascii=False,
                    ),
                },
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        return report


def export_system_prompts(old_engine: Engine, path: Path) -> int:
    """把旧库 is_system=True 提示词导出为种子 JSON（``--export-seed``），返回条数。

    导出后应人工核对并提交进仓库——此后该文件是系统提示词正文的唯一来源。
    """
    tables = _legacy_tables(old_engine)
    prompts_t = tables["custom_prompts"]
    with old_engine.connect() as conn:
        rows = conn.execute(
            select(
                prompts_t.c.name,
                prompts_t.c.stage,
                prompts_t.c.content,
                func.coalesce(prompts_t.c.is_default, True).label("is_default"),
                func.coalesce(prompts_t.c.is_active, True).label("is_active"),
            )
            .where(func.coalesce(prompts_t.c.is_system, False) == True)  # noqa: E712
            .order_by(prompts_t.c.id)
        ).mappings().all()
    data = {
        "source": (
            "T5 迁移导出自旧库 custom_prompts.is_system=True"
            f"（{_utcnow().isoformat(timespec='seconds')}）"
        ),
        "prompts": [
            {
                "name": row["name"],
                "stage": row["stage"],
                "content": row["content"],
                "is_default": bool(row["is_default"]),
                "is_active": bool(row["is_active"]),
            }
            for row in rows
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return len(data["prompts"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.migrate_old_db",
        description="旧库 → 新库一次性数据迁移（幂等，迁移后硬对账）",
    )
    parser.add_argument("legacy_db", help="旧版 SQLite 数据库文件路径")
    parser.add_argument(
        "--database-url",
        default=None,
        help="新库 SQLAlchemy URL（默认取 .env 的 DATABASE_URL）",
    )
    parser.add_argument(
        "--export-seed",
        nargs="?",
        const=str(SEED_FILE),
        default=None,
        metavar="PATH",
        help=f"先导出旧库 is_system 提示词为种子 JSON（默认 {SEED_FILE}），然后照常迁移",
    )
    args = parser.parse_args(argv)

    from app.config import settings
    from app.db import create_db_engine

    old_engine = create_db_engine(f"sqlite:///{Path(args.legacy_db)}")
    try:
        if args.export_seed:
            count = export_system_prompts(old_engine, Path(args.export_seed))
            print(f"已导出 {count} 条系统提示词种子 → {args.export_seed}（请核对后提交仓库）")

        new_engine = create_db_engine(args.database_url or settings.database_url)
        try:
            report = migrate_legacy_db(old_engine, new_engine)
        finally:
            new_engine.dispose()
    except MigrationError as exc:
        print(f"迁移中止：{exc}", file=sys.stderr)
        return 1
    finally:
        old_engine.dispose()

    if report.already_migrated:
        print(f"该新库已迁移过同一旧库快照（{report.source_fingerprint}），幂等跳过。")
        print(f"对账复核：{len(report.reconciliation)} 个用户 SUM(账本) == 余额 全部通过。")
        return 0

    print(f"旧库快照：{report.source_fingerprint}")
    print(
        f"迁入完成：用户 {report.users}，账本 {report.ledger_entries}，"
        f"自定义提示词 {report.custom_prompts}，排版规格 {report.saved_specs}，"
        f"注入系统提示词种子 {report.system_prompts_seeded}"
    )
    print("对账报告（逐用户 SUM(账本) == 余额）：")
    for user_id, card_key, ledger_sum, balance in report.reconciliation:
        print(f"  user_id={user_id} 卡密={card_key} SUM(账本)={ledger_sum} 余额={balance} ✓")
    print(f"共 {len(report.reconciliation)} 个用户全部对账通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
