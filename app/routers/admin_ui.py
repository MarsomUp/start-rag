"""管理端界面（HTMX）。

设计
----
- 页面本身只搭骨架，**数据全部由 HTMX 片段渲染**，服务端返回 HTML，前端几乎没有 JS。
- 上传和删除都返回"整个书籍列表"，直接替换 `#book-list`。
  这样不需要前端维护任何状态，也不会出现"删了一行但计数没变"这类不一致。
- 进度条不单独轮询 job，而是**整个列表按需刷新**：
  有任务在跑时片段里带 `hx-trigger="every 1s"`，空闲时降到 15s。
  轮询节奏由服务端决定，前端不用管。
- 业务逻辑复用 `services/ingestion.register_upload`，和 JSON 接口是同一份实现。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import JOB_QUEUED, JOB_RUNNING, Book, Job
from app.security import page_context, require_admin
from app.services import ingestion
from app.templating import templates
from app.worker import get_task_queue

# 整个管理端界面都要管理员。守卫写在 router 上，
# 以后新增片段路由不会漏掉 —— 未登录会被这里拦住并跳转到 /login。
router = APIRouter(
    tags=["admin-ui"],
    include_in_schema=False,
    dependencies=[Depends(require_admin)],
)


# ----------------------------------------------------------------------
# 组装片段所需的上下文
# ----------------------------------------------------------------------
def _list_context(db: Session) -> dict:
    """书籍列表 + 队列徽标所需的全部数据。

    为什么合成一个上下文：它们的响应也是合在一起的
    （见 `_book_list_response.html` 的带外交换）—— 这样整个管理端
    只有**一条**轮询循环，不会出现"列表安静了、徽标还在每 3 秒敲一次"。
    """
    books = list(db.scalars(select(Book).order_by(Book.id.desc())))

    # 每本书最近一次任务（按 id 递增遍历，后者覆盖前者）
    latest: dict[int, Job] = {}
    for job in db.scalars(select(Job).order_by(Job.id)):
        if job.book_id is not None:
            latest[job.book_id] = job

    active = any(j.status in (JOB_QUEUED, JOB_RUNNING) for j in latest.values())

    queued = db.scalar(
        select(func.count()).select_from(Job).where(Job.status == JOB_QUEUED)
    )
    running = db.scalars(
        select(Job).where(Job.status == JOB_RUNNING).order_by(Job.id).limit(1)
    ).first()

    return {
        "books": books,
        "jobs": latest,
        "active": active,
        # 有任务在跑就 1 秒刷一次，空闲了降到 15 秒，避免无意义轮询
        "poll_seconds": 1 if active else 15,
        # 队列徽标用
        "queued_count": int(queued or 0),
        "running_job": running,
        "worker_alive": get_task_queue().is_alive,
    }


# ----------------------------------------------------------------------
# 页面
# ----------------------------------------------------------------------
@router.get("/admin", response_class=HTMLResponse)
def page_admin(request: Request, db: Session = Depends(get_db)):
    context = page_context(request, db, settings=settings)
    context.update(_list_context(db))
    return templates.TemplateResponse(request, "admin.html", context)


# ----------------------------------------------------------------------
# 片段
# ----------------------------------------------------------------------
@router.get("/admin/partials/books", response_class=HTMLResponse)
def partial_books(request: Request, db: Session = Depends(get_db)):
    """书籍列表 + 队列徽标。带 `hx-trigger`，换上去之后会继续按节奏自我刷新。"""
    return templates.TemplateResponse(
        request, "_book_list_response.html", _list_context(db)
    )


@router.post("/admin/partials/books", response_class=HTMLResponse)
def partial_upload(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """上传。成功 → 返回新的书籍列表；失败 → 用 HX-Retarget 把错误打到表单下方。"""
    try:
        ingestion.register_upload(db, file.filename or "book.txt", file.file.read())
    except ingestion.UploadError as exc:
        response = templates.TemplateResponse(
            request, "_upload_error.html", {"message": exc.message}
        )
        # htmx 的响应头重定向：不改前端代码，就能把这次响应换到别的地方去
        response.headers["HX-Retarget"] = "#upload-error"
        response.headers["HX-Reswap"] = "innerHTML"
        return response

    return templates.TemplateResponse(
        request, "_book_list_response.html", _list_context(db)
    )


@router.delete("/admin/partials/books/{book_id}", response_class=HTMLResponse)
def partial_delete(request: Request, book_id: int, db: Session = Depends(get_db)):
    """删除。同样返回整个列表，前端不用自己摘行、自己改计数。

    只删记录和原始文件；`.rag_cache` 里的向量按内容哈希存着，故意保留 ——
    同一本书重新上传会立刻命中缓存。
    """
    book = db.get(Book, book_id)
    if book is not None:
        Path(book.file_path).unlink(missing_ok=True)
        db.delete(book)
        db.commit()
    return templates.TemplateResponse(
        request, "_book_list_response.html", _list_context(db)
    )
