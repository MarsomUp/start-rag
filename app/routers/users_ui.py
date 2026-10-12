"""用户管理界面（HTMX，仅管理员）。

和书籍列表有个重要区别：**这里不做自动轮询。**
用户不会自己冒出来，增删之后片段自己刷新就够了 ——
多一条常驻轮询没有任何收益（见 AGENTS.md 第 3 节第 7 条）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ROLE_USER, User
from app.security import get_current_user, page_context, require_admin
from app.services import users as users_service
from app.templating import templates

router = APIRouter(tags=["users-ui"], include_in_schema=False)


def _list_context(request: Request, db: Session) -> dict:
    """列表片段需要的上下文。

    `current_user` 是必须的 —— 表格要靠它把"自己"那一行的删除按钮藏掉
    （服务层也会拦，界面只是别让人点了才报错）。
    """
    return {
        "users": users_service.list_users(db),
        "current_user": get_current_user(request, db),
    }


def _error_response(request: Request, message: str, status_code: int = 200):
    """错误打进表单下方，而不是换掉整个列表。"""
    response = templates.TemplateResponse(
        request, "_user_error.html", {"message": message}, status_code=status_code
    )
    response.headers["HX-Retarget"] = "#user-error"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


@router.get("/users", response_class=HTMLResponse)
def page_users(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request, "users.html", page_context(request, db, **_list_context(request, db))
    )


@router.get("/users/partials/list", response_class=HTMLResponse)
def partial_list(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request, "_user_list.html", _list_context(request, db)
    )


@router.post("/users/partials/create", response_class=HTMLResponse)
def partial_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(ROLE_USER),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        users_service.create_user(db, username, password, role)
    except users_service.UserError as exc:
        return _error_response(request, exc.message)

    response = templates.TemplateResponse(
        request, "_user_list.html", _list_context(request, db)
    )
    # 让前端把表单清空（见 users.html 里的 hx-on::after-request）
    return response


@router.delete("/users/partials/{user_id}", response_class=HTMLResponse)
def partial_delete(
    request: Request,
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        users_service.delete_user(db, user_id, admin)
    except users_service.UserError as exc:
        return _error_response(request, exc.message)
    return templates.TemplateResponse(
        request, "_user_list.html", _list_context(request, db)
    )
