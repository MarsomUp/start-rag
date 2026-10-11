"""数据库连接与会话。

用 SQLite：零运维，够这个项目用很久；以后换 Postgres 只需要改连接串。

⚠️ 并发要点：API 线程和后台 Worker 会**同时写** jobs 表（Worker 上报进度，
API 读进度）。SQLite 默认会因为写锁冲突抛 "database is locked"，所以必须开：
    PRAGMA journal_mode = WAL    —— 读写可以并行
    PRAGMA busy_timeout = 5000   —— 抢不到锁就等 5 秒再报错，而不是立刻失败
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


settings.ensure_dirs()

engine = create_engine(
    f"sqlite:///{settings.db_path}",
    # FastAPI 的同步端点跑在线程池里，同一个连接可能被不同线程用到
    connect_args={"check_same_thread": False},
    future=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """建表。生产环境应该换成 Alembic 迁移，阶段 1 先用 create_all。"""
    from app import models  # noqa: F401  导入以注册所有模型

    Base.metadata.create_all(engine)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个 session。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
