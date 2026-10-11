"""用户端 JSON API：问答 + 可检索书籍列表。

业务逻辑在 services/chat.py，和 HTMX 界面共用。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import BookOut, ChatRequest, ChatResponse, SourceOut
from app.services import chat as chat_service

router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat", response_model=ChatResponse, summary="提问")
def chat(req: ChatRequest, db: Session = Depends(get_db)):
    try:
        result = chat_service.ask_question(db, req.question, req.book_ids, req.k)
    except chat_service.ChatError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return ChatResponse(
        answer=result.answer,
        sources=[
            SourceOut(index=s.index, book=s.book, snippet=s.snippet)
            for s in result.sources
        ],
        elapsed=result.elapsed,
        cache_status=result.cache_status,
    )


@router.get("/books", response_model=list[BookOut], summary="可检索的书籍列表")
def list_ready_books(db: Session = Depends(get_db)):
    """用户端只看到已就绪的书；管理端接口能看到全部状态。"""
    return chat_service.select_books(db)
