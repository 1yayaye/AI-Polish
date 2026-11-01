"""T5 数据迁移测试：按旧版 schema（git main:backend/app/models/models.py）造
合成旧库，测迁移的外部行为——全量迁入、旧会话引用的账本行、幂等重跑、
硬校验失败回滚、is_system 提示词导出与种子注入。

旧库夹具用裸 sqlite 引擎（不挂 app.db 的 PRAGMA）：旧库布尔列存 0/1/NULL、
且可能存在引用已删除会话的孤儿账本行，测试构造的就是这样的真实旧库。
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.models import CustomPrompt, LedgerEntry, SavedSpec, User
from app.seeds import load_system_prompts, seed_system_prompts
from scripts.migrate_old_db import (
    LEGACY_TASK_ID_OFFSET,
    MigrationError,
    export_system_prompts,
    main as cli_main,
    migrate_legacy_db,
)

# ---- 旧库 schema：列集对齐旧版 models.py，sqlite 类型写法对齐旧库实体文件 ----

_LEGACY_DDL = (
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        card_key VARCHAR(255) NOT NULL,
        access_link VARCHAR(255),
        is_active BOOLEAN,
        created_at DATETIME,
        last_used DATETIME,
        usage_limit INTEGER,
        usage_count INTEGER,
        workspace_balance_cents INTEGER,
        workspace_total_spent_cents INTEGER
    )
    """,
    "CREATE UNIQUE INDEX ix_users_card_key ON users (card_key)",
    """
    CREATE TABLE optimization_sessions (
        id INTEGER PRIMARY KEY,
        user_id INTEGER,
        session_id VARCHAR(255),
        status VARCHAR(50)
    )
    """,
    """
    CREATE TABLE billing_transactions (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users (id),
        optimization_session_id INTEGER REFERENCES optimization_sessions (id),
        transaction_type VARCHAR(50) NOT NULL,
        amount_cents INTEGER NOT NULL,
        balance_after_cents INTEGER NOT NULL,
        reason TEXT,
        created_at DATETIME
    )
    """,
    """
    CREATE TABLE custom_prompts (
        id INTEGER PRIMARY KEY,
        user_id INTEGER REFERENCES users (id),
        name VARCHAR(255) NOT NULL,
        stage VARCHAR(50) NOT NULL,
        content TEXT NOT NULL,
        is_default BOOLEAN,
        is_system BOOLEAN,
        is_active BOOLEAN,
        created_at DATETIME,
        updated_at DATETIME
    )
    """,
    """
    CREATE TABLE saved_specs (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users (id),
        name VARCHAR(100) NOT NULL,
        description VARCHAR(500),
        spec_json TEXT NOT NULL,
        created_at DATETIME,
        updated_at DATETIME
    )
    """,
)


@pytest.fixture()
def legacy_engine(tmp_path: Path) -> Engine:
    """按旧版 schema 建的合成旧库。裸引擎：不启用外键强制，允许孤儿引用。"""
    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    with engine.begin() as conn:
        for ddl in _LEGACY_DDL:
            conn.exec_driver_sql(ddl)
    yield engine
    engine.dispose()


def _add_legacy_user(conn, card_key: str, *, balance: int = 0, usage_limit: int = 0, usage_count: int = 0) -> int:
    result = conn.exec_driver_sql(
        "INSERT INTO users (card_key, is_active, usage_limit, usage_count,"
        " workspace_balance_cents) VALUES (?, 1, ?, ?, ?)",
        (card_key, usage_limit, usage_count, balance),
    )
    return result.lastrowid


def _add_legacy_session(conn, user_id: int) -> int:
    result = conn.exec_driver_sql(
        "INSERT INTO optimization_sessions (user_id, session_id, status) VALUES (?, ?, 'completed')",
        (user_id, f"legacy-session-{user_id}"),
    )
    return result.lastrowid


def _add_legacy_tx(
    conn,
    user_id: int,
    tx_type: str,
    amount: int,
    balance_after: int,
    *,
    session_id: int | None = None,
    reason: str | None = None,
    created_at: str = "2026-01-15 08:30:00",
) -> int:
    result = conn.exec_driver_sql(
        "INSERT INTO billing_transactions (user_id, optimization_session_id,"
        " transaction_type, amount_cents, balance_after_cents, reason, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, session_id, tx_type, amount, balance_after, reason, created_at),
    )
    return result.lastrowid


def _add_legacy_prompt(
    conn,
    *,
    user_id: int | None,
    name: str,
    stage: str = "polish",
    content: str = "提示词正文",
    is_default: bool = False,
    is_system: bool = False,
    is_active: bool = True,
) -> int:
    result = conn.exec_driver_sql(
        "INSERT INTO custom_prompts (user_id, name, stage, content, is_default,"
        " is_system, is_active) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, name, stage, content, int(is_default), int(is_system), int(is_active)),
    )
    return result.lastrowid


def _add_legacy_spec(conn, user_id: int, name: str = "课程报告规格") -> int:
    result = conn.exec_driver_sql(
        "INSERT INTO saved_specs (user_id, name, description, spec_json) VALUES (?, ?, ?, ?)",
        (user_id, name, None, json.dumps({"font": "宋体", "size_pt": 12}, ensure_ascii=False)),
    )
    return result.lastrowid


def _new_tables_count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


def _new_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _build_standard_legacy(engine: Engine) -> int:
    """两用户 + 全部三类账本流水（含真实会话引用与孤儿会话引用）+ 提示词 + 规格。

    u1：adjust +5000 → precharge -1200（引用真实旧会话）→ refund +1200
    （引用孤儿会话 424242，旧会话已被清理），账本合计 5000 == 余额。
    u2：余额 0、次数轨 usage_limit=100 / usage_used=37，无账本流水。
    返回真实旧会话 id，供断言 task_id 映射。
    """
    """两用户 + 全部三类账本流水（含真实会话引用与孤儿会话引用）+ 提示词 + 规格。

    u1：adjust +5000 → precharge -1200（引用真实旧会话）→ refund +1200
    （引用孤儿会话 424242，旧会话已被清理），账本合计 5000 == 余额。
    u2：余额 0、次数轨 usage_limit=100 / usage_used=37，无账本流水。
    """
    with engine.begin() as conn:
        u1 = _add_legacy_user(conn, "legacy-card-aaaaaaaa", balance=5000)
        u2 = _add_legacy_user(conn, "legacy-card-bbbbbbbb", usage_limit=100, usage_count=37)
        real_session = _add_legacy_session(conn, u1)
        _add_legacy_tx(conn, u1, "admin_adjustment", 5000, 5000, reason="充值")
        _add_legacy_tx(conn, u1, "workspace_precharge", -1200, 3800,
                       session_id=real_session, reason="workspace task precharge")
        _add_legacy_tx(conn, u1, "workspace_refund", 1200, 5000,
                       session_id=424242, reason=None)  # 孤儿旧会话引用
        _add_legacy_prompt(conn, user_id=u1, name="我的润色模板", is_default=True)
        _add_legacy_prompt(conn, user_id=None, name="全局公共模板", stage="enhance")
        _add_legacy_prompt(conn, user_id=None, name="系统预设（不随数据迁）", is_system=True)
        _add_legacy_spec(conn, u1)
        return real_session


# ---- 迁移行为 ----


def test_full_migration_row_counts_and_values(db_engine: Engine, legacy_engine: Engine):
    real_session_id = _build_standard_legacy(legacy_engine)

    report = migrate_legacy_db(legacy_engine, db_engine)

    assert (report.users, report.ledger_entries, report.custom_prompts,
            report.saved_specs) == (2, 3, 2, 1)
    assert report.already_migrated is False
    # 系统提示词不作为数据迁移，由代码种子注入
    assert report.system_prompts_seeded == len(load_system_prompts())

    session = _new_session_factory(db_engine)()
    try:
        users = {u.card_key: u for u in session.query(User).all()}
        assert set(users) == {"legacy-card-aaaaaaaa", "legacy-card-bbbbbbbb"}
        u1, u2 = users["legacy-card-aaaaaaaa"], users["legacy-card-bbbbbbbb"]
        # 双轨额度逐字段对上旧库
        assert (u1.balance_fen, u1.usage_limit, u1.usage_used) == (5000, 0, 0)
        assert (u2.balance_fen, u2.usage_limit, u2.usage_used) == (0, 100, 37)

        entries = session.query(LedgerEntry).order_by(LedgerEntry.id).all()
        by_kind = {e.kind: e for e in entries}
        assert {e.kind for e in entries} == {"adjust", "charge", "refund"}
        # 类型与金额逐行对上旧账本（含带符号增量与变动后余额）
        assert by_kind["adjust"].amount_fen == 5000
        assert by_kind["adjust"].balance_after_fen == 5000
        assert by_kind["charge"].amount_fen == -1200
        assert by_kind["charge"].balance_after_fen == 3800
        assert by_kind["refund"].amount_fen == 1200
        # 次数轨无历史：旧流水一律不写 usage_after
        assert all(e.usage_after is None for e in entries)
        # 旧会话引用 → 无外键 task_id，统一偏移；孤儿会话引用同样不断裂
        assert by_kind["refund"].task_id == 424242 + LEGACY_TASK_ID_OFFSET
        assert by_kind["charge"].task_id == real_session_id + LEGACY_TASK_ID_OFFSET
        # 旧 reason 原样保留；NULL reason 落成带旧类型的迁移说明
        assert by_kind["adjust"].reason == "充值"
        assert "workspace_refund" in by_kind["refund"].reason
        # 旧时间戳逐行保留
        assert by_kind["adjust"].created_at.strftime("%Y-%m-%d %H:%M:%S") == "2026-01-15 08:30:00"

        # 用户自定义提示词与规格条数一致；is_system 行只以种子形式存在
        prompts = session.query(CustomPrompt).all()
        user_prompts = [p for p in prompts if not p.is_system]
        assert [p.name for p in user_prompts] == ["我的润色模板", "全局公共模板"]
        assert user_prompts[1].user_id is None and user_prompts[0].user_id == u1.id
        seeded = [p for p in prompts if p.is_system]
        assert {p.name for p in seeded} == {p["name"] for p in load_system_prompts()}
        assert all(p.user_id is None for p in seeded)
        specs = session.query(SavedSpec).all()
        assert len(specs) == 1 and json.loads(specs[0].spec_json)["font"] == "宋体"
    finally:
        session.close()

    # 对账行：每个用户的 SUM(账本) == 余额
    assert report.reconciliation == [
        (u1.id, "lega****aaaa", 5000, 5000),
        (u2.id, "lega****bbbb", 0, 0),
    ]


def test_ledger_row_with_missing_legacy_session_migrates(db_engine: Engine, legacy_engine: Engine):
    """账本行引用的旧会话在旧库里已不存在：迁移成功，task_id 保留可追溯。"""
    with legacy_engine.begin() as conn:
        user_id = _add_legacy_user(conn, "legacy-card-orphan", balance=800)
        _add_legacy_tx(conn, user_id, "admin_adjustment", 800, 800, reason="充值")
        _add_legacy_tx(conn, user_id, "workspace_precharge", -300, 500,
                       session_id=999_999)  # 旧会话已清理，只剩账本引用
        _add_legacy_tx(conn, user_id, "workspace_refund", 300, 800,
                       session_id=999_999)

    migrate_legacy_db(legacy_engine, db_engine)

    session = _new_session_factory(db_engine)()
    try:
        charge = session.query(LedgerEntry).filter_by(kind="charge").one()
        assert charge.task_id == 999_999 + LEGACY_TASK_ID_OFFSET
        assert charge.amount_fen == -300 and charge.balance_after_fen == 500
        refund = session.query(LedgerEntry).filter_by(kind="refund").one()
        assert refund.task_id == charge.task_id  # 同一旧会话的扣费与退款同源
        assert session.query(User).one().balance_fen == 800
    finally:
        session.close()


def test_rerun_is_idempotent(db_engine: Engine, legacy_engine: Engine, tmp_path: Path):
    """重复执行不产生重复数据：第二次运行幂等跳过，各表条数不变。"""
    legacy_path = tmp_path / "legacy.db"  # legacy_engine 由夹具建好，路径仅供 CLI 用
    _build_standard_legacy(legacy_engine)
    db_url = db_engine.url.render_as_string(hide_password=False)

    first = migrate_legacy_db(legacy_engine, db_engine)
    assert first.already_migrated is False

    counts_before = {
        table: _new_tables_count(db_engine, table)
        for table in ("users", "ledger_entries", "custom_prompts", "saved_specs")
    }

    # 同一快照重跑（走 CLI，验证退出码与跳过路径）
    exit_code = cli_main([str(legacy_path), "--database-url", db_url])
    assert exit_code == 0

    counts_after = {
        table: _new_tables_count(db_engine, table)
        for table in ("users", "ledger_entries", "custom_prompts", "saved_specs")
    }
    assert counts_after == counts_before

    second = migrate_legacy_db(legacy_engine, db_engine)
    assert second.already_migrated is True
    assert second.users == 0  # 跳过路径不做任何写入
    assert [(uid, s, b) for uid, _, s, b in second.reconciliation] == [
        (uid, s, b) for uid, _, s, b in first.reconciliation
    ]


def test_reconciliation_failure_rolls_back_everything(db_engine: Engine, legacy_engine: Engine, capsys):
    """旧库本身账实不符（余额 5000、账本合计 3800）：硬校验不过 → 报差异、整体回滚。"""
    with legacy_engine.begin() as conn:
        user_id = _add_legacy_user(conn, "legacy-card-drift", balance=5000)
        _add_legacy_tx(conn, user_id, "admin_adjustment", 3800, 3800, reason="充值")

    with pytest.raises(MigrationError) as exc_info:
        migrate_legacy_db(legacy_engine, db_engine)
    message = str(exc_info.value)
    assert "SUM(账本)" in message and "5000" in message and "3800" in message
    assert "lega****rift" in message  # 差异明细指到具体用户（卡密脱敏展示）

    # 新库回到迁移前状态：没有任何数据、没有迁移标记，可安全重跑
    assert _new_tables_count(db_engine, "users") == 0
    assert _new_tables_count(db_engine, "ledger_entries") == 0
    assert _new_tables_count(db_engine, "custom_prompts") == 0
    assert _new_tables_count(db_engine, "saved_specs") == 0
    with db_engine.connect() as conn:
        marker = conn.execute(
            text("SELECT COUNT(*) FROM migration_state WHERE key = 'legacy_migration_v1'")
        ).scalar_one()
    assert marker == 0
    assert capsys.readouterr().err == ""  # 核心函数不直接打印，报告经异常/返回值交付


def test_unknown_legacy_transaction_type_aborts(db_engine: Engine, legacy_engine: Engine):
    with legacy_engine.begin() as conn:
        user_id = _add_legacy_user(conn, "legacy-card-x", balance=100)
        _add_legacy_tx(conn, user_id, "mystery_type", 100, 100)

    with pytest.raises(MigrationError, match="mystery_type"):
        migrate_legacy_db(legacy_engine, db_engine)
    assert _new_tables_count(db_engine, "users") == 0
    assert _new_tables_count(db_engine, "ledger_entries") == 0


def test_snapshot_change_is_rejected(db_engine: Engine, legacy_engine: Engine):
    """同一新库换一份旧库快照重跑：拒绝，交人工裁决（防误迁错库）。"""
    with legacy_engine.begin() as conn:
        user_id = _add_legacy_user(conn, "legacy-card-a", balance=100)
        _add_legacy_tx(conn, user_id, "admin_adjustment", 100, 100)
    migrate_legacy_db(legacy_engine, db_engine)

    with legacy_engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM billing_transactions")
        user_id = _add_legacy_user(conn, "legacy-card-b", balance=200)
        _add_legacy_tx(conn, user_id, "admin_adjustment", 200, 200)

    with pytest.raises(MigrationError, match="快照"):
        migrate_legacy_db(legacy_engine, db_engine)
    # 第一次迁移的数据不受影响
    assert _new_tables_count(db_engine, "users") == 1


def test_cli_failure_exit_code_and_stderr(db_engine: Engine, legacy_engine: Engine, tmp_path: Path, capsys):
    with legacy_engine.begin() as conn:
        user_id = _add_legacy_user(conn, "legacy-card-drift", balance=5000)
        _add_legacy_tx(conn, user_id, "admin_adjustment", 3800, 3800)

    exit_code = cli_main([
        str(tmp_path / "legacy.db"),
        "--database-url", db_engine.url.render_as_string(hide_password=False),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "迁移中止" in captured.err
    assert "差异明细" in captured.err


# ---- is_system 提示词：导出与种子注入 ----


def test_export_system_prompts_writes_seed_json(legacy_engine: Engine, tmp_path: Path):
    with legacy_engine.begin() as conn:
        user_id = _add_legacy_user(conn, "legacy-card-p")
        _add_legacy_prompt(conn, user_id=user_id, name="用户自建", content="用户内容")
        _add_legacy_prompt(conn, user_id=None, name="默认润色提示词", stage="polish",
                           content="运营商调优过的润色正文", is_default=True, is_system=True)
        _add_legacy_prompt(conn, user_id=None, name="默认增强提示词", stage="enhance",
                           content="运营商调优过的增强正文", is_default=True, is_system=True,
                           is_active=False)

    out_path = tmp_path / "exported" / "system_prompts.json"
    count = export_system_prompts(legacy_engine, out_path)

    assert count == 2
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert [p["name"] for p in data["prompts"]] == ["默认润色提示词", "默认增强提示词"]
    assert data["prompts"][0]["content"] == "运营商调优过的润色正文"
    assert data["prompts"][1]["is_active"] is False
    # 导出物可被种子加载器读取（同一格式契约）
    assert [p["name"] for p in load_system_prompts(out_path)] == ["默认润色提示词", "默认增强提示词"]


def test_seed_system_prompts_into_empty_db_is_idempotent(db_engine: Engine):
    factory = _new_session_factory(db_engine)

    session = factory()
    try:
        inserted = seed_system_prompts(session)
        session.commit()
    finally:
        session.close()
    assert inserted == len(load_system_prompts())

    # 重跑不重复；运营在库内改过的正文不被种子覆盖（种子只管"从无到有"）
    session = factory()
    try:
        session.query(CustomPrompt).filter_by(is_system=True).update(
            {CustomPrompt.content: "运营手工改过的正文"}, synchronize_session=False
        )
        session.commit()
        assert seed_system_prompts(session) == 0
        session.commit()
        prompts = session.query(CustomPrompt).filter_by(is_system=True).all()
        assert {p.content for p in prompts} == {"运营手工改过的正文"}
        assert all(p.user_id is None for p in prompts)
    finally:
        session.close()


def test_committed_seed_file_loads_and_seeds(db_engine: Engine):
    """仓库里的种子文件本身可用：重建空库后一次注入即成。"""
    prompts = load_system_prompts()
    assert prompts, "仓库种子文件不应为空"
    assert {p["stage"] for p in prompts} <= {"polish", "enhance"}
    assert all(p["name"] and p["content"] for p in prompts)

    session = _new_session_factory(db_engine)()
    try:
        assert seed_system_prompts(session, prompts) == len(prompts)
        session.commit()
        assert session.query(CustomPrompt).filter_by(is_system=True).count() == len(prompts)
    finally:
        session.close()


def test_new_tables_in_alembic_head(db_engine: Engine):
    """custom_prompts / saved_specs 已进 Alembic 迁移链（迁移目标有表可落）。"""
    inspector = inspect(db_engine)
    assert {"custom_prompts", "saved_specs"} <= set(inspector.get_table_names())
