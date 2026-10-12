"""API 的请求 / 响应模型。

和 ORM 模型分开：数据库结构变了不一定想让 API 跟着变。
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ----------------------------------------------------------------------
# 书籍
# ----------------------------------------------------------------------
class BookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    filename: str
    status: str
    n_chunks: int | None = None
    dim: int | None = None
    model_name: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class UploadResponse(BaseModel):
    book_id: int
    job_id: int
    status: str
    dedup: bool = False
    """True 表示这本书之前已经向量化过，直接复用了缓存（此时不会真的跑任务）。"""
    queued: bool = False
    """True 表示已加入后台队列，需要用 GET /api/admin/jobs/{job_id} 轮询进度。"""
    message: str


# ----------------------------------------------------------------------
# 任务
# ----------------------------------------------------------------------
class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: str
    book_id: int | None = None
    status: str
    progress: int
    total: int
    message: str | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


# ----------------------------------------------------------------------
# 问答（阶段 4 会用）
# ----------------------------------------------------------------------
class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    book_ids: list[int] | None = None
    """不传表示使用全部 ready 的书。"""
    k: int | None = None


class SourceOut(BaseModel):
    index: int
    book: str | None = None
    snippet: str


class ChatResponse(BaseModel):
    answer: str
    sources: list[SourceOut]
    elapsed: float
    cache_status: str


# ----------------------------------------------------------------------
# 用户（阶段 5）
# ----------------------------------------------------------------------
class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: str
    created_at: datetime
    # 注意：**绝不出现在响应里**的字段是 password_hash —— 这就是要把 ORM 模型
    # 和 API 模型分开的原因，少写一个字段就漏出去了。


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$")
    """只允许 ASCII 字母数字和 _ . -，避免前后空格/全角字符这类看不见的坑。"""
    password: str = Field(min_length=6, max_length=64)
    role: Literal["admin", "user"] = "user"


class PasswordReset(BaseModel):
    password: str = Field(min_length=6, max_length=64)
