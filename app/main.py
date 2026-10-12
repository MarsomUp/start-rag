"""FastAPI 应用入口。

启动：
    python -m uvicorn app.main:app --reload
    或直接双击 start.bat / 执行 ./start.sh

页面：
    /        落地页
    /login   登录
    /chat    问答（需登录）
    /admin   书籍管理（需管理员）
    /users   用户管理（需管理员）
    /docs    自动生成的 API 文档
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.db import SessionLocal, init_db
from app.routers import admin_ui, auth_ui, books, chat, chat_ui, users, users_ui
from app.security import (
    AuthRequired,
    PermissionDenied,
    ensure_admin_user,
    get_session_secret,
    is_htmx,
    login_redirect_url,
    page_context,
    wants_html,
)
from app.templating import WEB_DIR, templates
from app.worker import get_task_queue

# 打开日志，否则后台 Worker 的 INFO 级日志（"开始处理任务 x"）根本不会打印，
# 你会以为后台什么都没发生。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动时建目录、建表、引导管理员，并拉起后台 Worker。

    注意这里**不加载向量模型** —— 模型是 lazy 的，第一次真正用到才加载，
    这样服务启动很快，没配 API Key 也能起来做上传。

    Worker 是进程内的后台线程（app/worker.py）：
      - 它串行消费 jobs 表里的任务，一次只跑一个（向量化吃满 4 核，不能并发）
      - 关服务时会被一起停掉；没跑完的任务下次启动会自动重新排队，从断点续算
    """
    settings.ensure_dirs()
    init_db()

    # 首次启动（users 表为空）建一个管理员，否则没人能登录进去。
    with SessionLocal() as db:
        new_admin, generated_password = ensure_admin_user(db)

    if new_admin is not None:
        print("\n" + "=" * 64, flush=True)
        print(f"  已创建管理员账号：{new_admin.username}", flush=True)
        if generated_password:
            print(f"  初始密码：{generated_password}", flush=True)
            print("  ⚠️  这个密码只打印这一次 —— 现在就去记下来，", flush=True)
            print("     或者在「用户管理」里给别的账号改密码后删掉它。", flush=True)
            print(f"     想自己指定：设置环境变量 RAG_ADMIN_PASSWORD 后删掉 "
                  f"{settings.db_path} 重启。", flush=True)
        else:
            print("  密码取自环境变量 RAG_ADMIN_PASSWORD。", flush=True)
        print("=" * 64 + "\n", flush=True)

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

# ----------------------------------------------------------------------
# 会话
#
# 用签名 cookie（服务端不存任何东西），内容只有 {"uid": 1}。
# 刻意不放 role —— 那样改了用户角色要等重新登录才生效。
# 详见 app/security.py 顶部的说明。
# ----------------------------------------------------------------------
app.add_middleware(
    SessionMiddleware,
    secret_key=get_session_secret(),
    session_cookie=settings.rag_session_cookie,
    max_age=settings.rag_session_max_age,
    same_site="lax",      # 挡住跨站表单提交（CSRF）
    https_only=False,     # 本地是 http 调试；上 HTTPS 后应改成 True
)

app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

# JSON API（给程序调用，也有 /docs 文档）。权限守卫在各自的 router 上。
app.include_router(books.router)
app.include_router(chat.router)
app.include_router(users.router)
# HTMX 界面（返回 HTML 片段，不进 OpenAPI 文档）
app.include_router(auth_ui.router)
app.include_router(admin_ui.router)
app.include_router(chat_ui.router)
app.include_router(users_ui.router)


# ----------------------------------------------------------------------
# 认证异常的响应
#
# 关键点：**同一个"没登录"要按请求类型给三种不同响应**。
# 对所有请求都 302 是不行的 —— htmx 会把登录页当成"响应内容"
# swap 进目标元素，界面上就出现一块莫名其妙的登录表单。
# ----------------------------------------------------------------------
@app.exception_handler(AuthRequired)
async def handle_auth_required(request: Request, _exc: AuthRequired):
    target = login_redirect_url(request)
    if is_htmx(request):
        # 让 htmx 放弃 swap，改为让浏览器整页跳转
        return HTMLResponse("", status_code=200, headers={"HX-Redirect": target})
    if wants_html(request):
        return RedirectResponse(target, status_code=303)
    return JSONResponse({"detail": "未登录"}, status_code=401)


@app.exception_handler(PermissionDenied)
async def handle_permission_denied(request: Request, _exc: PermissionDenied):
    if is_htmx(request):
        return HTMLResponse(
            '<div class="alert err-text">没有权限：这个操作需要管理员。</div>',
            status_code=403,
        )
    if wants_html(request):
        with SessionLocal() as db:
            ctx = page_context(request, db)
        return templates.TemplateResponse(request, "forbidden.html", ctx, status_code=403)
    return JSONResponse({"detail": "需要管理员权限"}, status_code=403)


# ----------------------------------------------------------------------
# 页面
# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def page_index(request: Request):
    with SessionLocal() as db:
        ctx = page_context(request, db, settings=settings)
    return templates.TemplateResponse(request, "index.html", ctx)


# ----------------------------------------------------------------------
# 健康检查（公开：方便脚本探活，不暴露任何业务数据）
# ----------------------------------------------------------------------
@app.get("/api/health", tags=["system"])
def health():
    return {
        "status": "ok",
        "model": settings.model_name,
        "db": str(settings.db_path),
        "books_dir": str(settings.books_dir),
    }
