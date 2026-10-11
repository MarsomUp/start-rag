"""用户端问答界面（HTMX）。

和阶段 3 的管理端用同一套模式：服务端返回 HTML 片段，前端几乎没有 JS。
- 提问：`hx-post` 到片段路由，回答以"一问一答"的卡片追加到会话区
- 引用来源：用原生 `<details>` 折叠，不需要 JS
- 等待反馈：`hx-indicator` 显示"正在检索并生成…"

业务逻辑复用 services/chat.ask_question，和 JSON 接口是同一份实现。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.services import chat as chat_service
from app.templating import templates

router = APIRouter(tags=["chat-ui"], include_in_schema=False)


@router.get("/chat", response_class=HTMLResponse)
def page_chat(request: Request, db: Session = Depends(get_db)):
    books = chat_service.select_books(db)
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "settings": settings,
            "books": books,
            # 服务端先检查一次，让用户在提问之前就知道 Key 没配
            "api_key_ok": bool(settings.deepseek_api_key),
        },
    )


@router.post("/chat/partials/ask", response_class=HTMLResponse)
def partial_ask(
    request: Request,
    question: str = Form(...),
    book_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """返回一张"一问一答"卡片，由 htmx 追加到会话区。

    失败也返回 200 + 一张错误卡片 —— 这样错误会出现在会话流里，
    和正常回答长得一样，用户不用去看浏览器控制台。
    """
    try:
        result = chat_service.ask_question(db, question, book_ids or None)
    except chat_service.ChatError as exc:
        return templates.TemplateResponse(
            request, "_chat_error.html", {"question": question, "message": exc.message}
        )

    return templates.TemplateResponse(request, "_exchange.html", {"result": result})
