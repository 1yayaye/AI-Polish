"""数据访问层基座：引擎创建、SQLite PRAGMA 固化、会话管理。

本模块拥有全部"怎么连数据库"的知识（PRAGMA、连接池、URL 解析），
调用方只需要 create_db_engine(url) 和 get_session()，不接触 sqlite3 细节。

## 短事务纪律（数据访问层约定，所有数据访问代码必须遵守）

- 事务只在"读/写一批相关行"期间打开，随用随开、即完即提交（默认 session
  生命周期即一个短事务单元，见 get_session）。
- **禁止在任何外部 I/O（尤其是 LLM 网络等待）期间持有会话或写事务**：
  先读出所需数据、提交并关闭会话，再发起外部调用，结果回来后开新短事务写回。
  这是单进程 asyncio + SQLite 永不 `database is locked` 的前提（rewrite-plan §5）。
- 涉及余额/次数的资金变动只允许条件原子 UPDATE + rowcount 校验
  （M1/T4 计费服务收口），禁止先读后写的 ORM 赋值路径。
"""

from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import settings

# mmap_size ≈ 30MB（rewrite-plan §5）：512MB 内存机器上限制 SQLite 的
# 内存映射页大小，防内存爆；其余 PRAGMA 依据 ADR-0001。
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",  # 读者不阻塞写者；WAL 会持久化进库文件，仍每次设置以求自洽
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA mmap_size=30720000",
)


def _wire_sqlite_pragmas(engine: Engine) -> None:
    """在每个新连接上固化全套 PRAGMA（连接事件触发，应用侧无法绕过）。"""

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        for pragma in _PRAGMAS:
            cursor.execute(pragma)
        cursor.close()


def create_db_engine(database_url: str) -> Engine:
    """按服务约定创建 Engine：调用方给 URL，其余（PRAGMA、连接池）由本模块决定。"""
    engine = create_engine(
        database_url,
        # NullPool：每次 checkout 新建连接。SQLite 建连成本极低，换来的是
        # 连接不跨线程复用（线程安全）且不存在陈旧连接回收问题。
        poolclass=NullPool,
    )
    _wire_sqlite_pragmas(engine)
    return engine


# 生产用引擎：进程启动时按 .env 的 DATABASE_URL 创建（ADR-0005）。
# SQLAlchemy 惰性建连，导入本模块不会触碰数据库文件。
engine = create_db_engine(settings.database_url)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI 依赖：一个请求一个短事务单元，请求结束即提交/回滚并关闭。

    请求处理函数不要在本会话跨越外部 I/O（见模块级短事务纪律）。
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
