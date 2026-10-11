"""FastAPI 应用入口。

启动：
    python -m uvicorn app.main:app --reload
    或直接双击 start.bat / 执行 ./start.sh

页面：
    /        落地页
    /admin   管理端（HTMX，见 app/routers/admin_ui.py）
    /docs    自动生成的 API 文档
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import init_db
from app.routers import admin_ui, books, chat, chat_ui
from app.templating import WEB_DIR, templates
from app.worker import get_task_queue

# 打开日志，否则后台 Worker 的 INFO 级日志（"开始处理任务 x"）根本不会打印，
# 你会以为后台什么都没发生。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动时建目录、建表，并拉起后台 Worker。

    注意这里**不加载向量模型** —— 模型是 lazy 的，第一次真正用到才加载，
    这样服务启动很快，没配 API Key 也能起来做上传。

    Worker 是进程内的后台线程（app/worker.py）：
      - 它串行消费 jobs 表里的任务，一次只跑一个（向量化吃满 4 核，不能并发）
      - 关服务时会被一起停掉；没跑完的任务下次启动会自动重新排队，从断点续算
    """
    settings.ensure_dirs()
    init_db()

    queue = get_task_queue()
    queue.start()
    try:
        yield
    finally:
        queue.stop()


app = FastAPI(
    title="知识库问答系统",
    description="基于 RAG 的企业知识库问答：管理端上传书籍，用户端提问。",
    version="0.1.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

# JSON API（给程序调用，也有 /docs 文档）
app.include_router(books.router)
app.include_router(chat.router)
# HTMX 界面（返回 HTML 片段，不进 OpenAPI 文档）
app.include_router(admin_ui.router)
app.include_router(chat_ui.router)


# ----------------------------------------------------------------------
# 页面
# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def page_index(request: Request):
    return templates.TemplateResponse(
        request, "index.html", {"settings": settings}
    )


# ----------------------------------------------------------------------
# 健康检查
# ----------------------------------------------------------------------
@app.get("/api/health", tags=["system"])
def health():
    return {
        "status": "ok",
        "model": settings.model_name,
        "db": str(settings.db_path),
        "books_dir": str(settings.books_dir),
    }
