"""问答服务：选书 → 检索 → 生成 → 记录。

JSON 接口（/api/chat）和 HTMX 界面（/chat/partials/ask）共用这一份实现。

注意 `RagEngine.ask()` 是**同步阻塞**的（要跑一次向量化 + 一次 LLM 调用），
所以调用它的路由必须是 `def` 而不是 `async def` —— FastAPI 会把 `def` 端点
丢进线程池，不会卡住事件循环。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BOOK_READY, Book, ChatLog
from app.services.rag import Source, get_engine


class ChatError(Exception):
    """问答失败。服务层不关心 HTTP，由路由层翻译成对应响应。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class ChatResult:
    question: str
    answer: str
    sources: list[Source] = field(default_factory=list)
    elapsed: float = 0.0
    cache_status: str = ""
    books: list[Book] = field(default_factory=list)


def select_books(db: Session, book_ids: list[int] | None = None) -> list[Book]:
    """挑出可检索的书。

    只认 status=ready 的 —— 还在向量化或失败的书不能参与检索。
    book_ids 为空表示"全部就绪的书"。
    """
    stmt = select(Book).where(Book.status == BOOK_READY).order_by(Book.id)
    if book_ids:
        stmt = stmt.where(Book.id.in_(book_ids))
    return list(db.scalars(stmt))


def ask_question(
    db: Session,
    question: str,
    book_ids: list[int] | None = None,
    k: int | None = None,
) -> ChatResult:
    """完整走一遍 RAG 并记账。

    Raises:
        ChatError: 问题为空 / 没有可检索的书 / 引擎或模型调用失败
    """
    question = (question or "").strip()
    if not question:
        raise ChatError("问题不能为空")

    books = select_books(db, book_ids)
    if not books:
        raise ChatError("还没有已就绪的书籍。请先到管理端上传，等向量化完成后再提问。")

    # 引擎是懒加载单例：第一次提问会加载向量模型，会慢十几秒
    try:
        engine = get_engine()
    except Exception as exc:  # noqa: BLE001  配置问题（比如没配 API Key）
        raise ChatError(
            f"RAG 引擎初始化失败：{type(exc).__name__}: {exc}", status_code=503
        ) from exc

    files = [b.file_path for b in books]
    try:
        answer = engine.ask(
            question,
            files,
            k=k,
            with_sources=True,
            book_names=[b.title for b in books],
        )
    except Exception as exc:  # noqa: BLE001  网络/鉴权/额度等
        raise ChatError(
            f"生成回答失败：{type(exc).__name__}: {exc}", status_code=502
        ) from exc

    # ------------------------------------------------------------------
    # 把引用来源里的书名换成给人看的那种
    #
    # rag_cache 给每个块打的 book 标签是「文件 stem」。上传的文件在磁盘上叫
    # `<sha12>_原名.txt`（用内容哈希前缀防同名覆盖），所以 stem 会是
    # `48922aa73c07_三国演义` —— 直接显示很难看。
    # 这里映射回数据库里的 title，同时也对之前已经缓存好的旧数据生效。
    # ------------------------------------------------------------------
    title_by_stem = {Path(b.file_path).stem: b.title for b in books}
    for s in answer.sources:
        if s.book:
            s.book = title_by_stem.get(s.book, s.book)

    # 记一笔，方便以后做历史记录和统计（阶段 5 会把 user_id 填上）
    db.add(
        ChatLog(
            question=question,
            answer=answer.text,
            sources_json=json.dumps(
                [
                    {"index": s.index, "book": s.book, "snippet": s.snippet}
                    for s in answer.sources
                ],
                ensure_ascii=False,
            ),
            elapsed_ms=int(answer.elapsed * 1000),
        )
    )
    db.commit()

    return ChatResult(
        question=question,
        answer=answer.text,
        sources=answer.sources,
        elapsed=answer.elapsed,
        cache_status=answer.cache_status,
        books=books,
    )
