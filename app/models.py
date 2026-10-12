"""ORM 模型。

设计要点：books.file_sha256 与 rag_cache 的缓存键天然对齐 ——
同一本书重复上传时，命中缓存直接复用，不重复向量化（这是"单独加载"能快起来的原因）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# 书籍状态机
BOOK_UPLOADED = "uploaded"      # 文件已落盘，还没开始向量化
BOOK_PROCESSING = "processing"  # 正在向量化
BOOK_READY = "ready"            # 可以检索
BOOK_FAILED = "failed"

# 任务状态机
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_CANCELED = "canceled"

# 角色
ROLE_ADMIN = "admin"   # 能进管理端：上传/删除书籍、管理用户
ROLE_USER = "user"     # 只能用问答


class Book(Base):
    __tablename__ = "books"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(1024))
    file_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)

    status: Mapped[str] = mapped_column(String(16), default=BOOK_UPLOADED, index=True)

    # 向量化结果（就绪后填充）
    n_chunks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    chunk_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_overlap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_key: Mapped[str | None] = mapped_column(String(32), nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    jobs: Mapped[list["Job"]] = relationship(
        back_populates="book", cascade="all, delete-orphan"
    )


class Job(Base):
    """一次异步任务。进度写在这张表里，前端轮询它。

    为什么必须落库：API 重启不能丢任务，而且 Worker 和 API 可能是两个进程，
    进度只能通过共享存储传递。
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(16), default="ingest")
    book_id: Mapped[int | None] = mapped_column(
        ForeignKey("books.id", ondelete="CASCADE"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default=JOB_QUEUED, index=True)

    progress: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    book: Mapped[Book | None] = relationship(back_populates="jobs")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user")  # admin | user
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChatLog(Base):
    __tablename__ = "chat_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    sources_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
