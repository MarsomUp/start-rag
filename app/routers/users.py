"""用户管理 JSON API —— **整个路由都要求管理员**。

守卫写在 `APIRouter(dependencies=...)` 上，而不是每个函数各写一遍：
这样以后新增接口时**不可能忘记加权限** —— 忘记加参数是常见疏漏，
忘记加路由级依赖则一眼就能看出来。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.schemas import PasswordReset, UserCreate, UserOut
from app.security import require_admin
from app.services import users as users_service

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


@router.get("/users", response_model=list[UserOut], summary="用户列表")
def list_users(db: Session = Depends(get_db)):
    return users_service.list_users(db)


@router.post("/users", response_model=UserOut, status_code=201, summary="新建用户")
def create_user(payload: UserCreate, db: Session = Depends(get_db)):
    try:
        return users_service.create_user(
            db, payload.username, payload.password, payload.role
        )
    except users_service.UserError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/users/{user_id}/password", response_model=UserOut, summary="重置密码")
def reset_password(user_id: int, payload: PasswordReset, db: Session = Depends(get_db)):
    try:
        return users_service.reset_password(db, user_id, payload.password)
    except users_service.UserError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.delete("/users/{user_id}", status_code=204, summary="删除用户")
def delete_user(
    user_id: int,
    # 删除需要知道"是谁在删"，所以这里再取一次当前用户
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        users_service.delete_user(db, user_id, admin)
    except users_service.UserError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
