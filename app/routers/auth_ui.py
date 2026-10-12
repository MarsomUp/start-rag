"""登录 / 登出。

**这里刻意不用 htmx。** 登录必然导致整页变化 —— 导航栏、能访问的入口都变了，
用普通表单 `POST` + 303 跳转最直接，而且完全不依赖 JS。
用 `hx-post` 反而要额外处理"这次响应到底是登录页还是目标页"。

失败时重新渲染登录页（HTTP 200 + 错误提示），这是表单类页面的惯例 ——
Django 也这么做。返回 401 会让某些代理/杀软插一脚，而且对浏览器没有额外意义。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import (
    get_current_user,
    login_user,
    logout_user,
    page_context,
    safe_next,
    verify_password,
)
from app.templating import templates

router = APIRouter(tags=["auth"], include_in_schema=False)


@router.get("/login", response_class=HTMLResponse)
def page_login(request: Request, next: str = "/", db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request, "login.html", page_context(request, db, next=safe_next(next))
    )


@router.post("/login", response_class=HTMLResponse)
def do_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    username = username.strip()
    user = db.scalars(select(User).where(User.username == username)).first()

    # 刻意不区分「用户名不存在」和「密码错误」——
    # 分开提示等于给人一个免费的账号枚举接口。
    if user is None or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            page_context(
                request, db,
                next=safe_next(next),
                username=username,
                error="用户名或密码不正确",
            ),
        )

    login_user(request, user)
    # 303 See Other：告诉浏览器"用 GET 去访问这个地址"，
    # 避免用户刷新时重复提交登录表单。
    return RedirectResponse(safe_next(next), status_code=303)


@router.post("/logout")
def do_logout(request: Request):
    """用 POST 而不是 GET：GET 会被浏览器**预取**，
    也可能被别的页面用 <img src="/logout"> 触发，导致用户莫名其妙掉线。"""
    logout_user(request)
    return RedirectResponse("/login", status_code=303)
