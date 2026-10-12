"""用户管理。

JSON 接口（/api/admin/users）和 HTMX 界面（/users）共用这一份实现 ——
和 services/ingestion.py、services/chat.py 是同一个模式：
service 抛自定义异常，路由层负责把它翻译成 HTTPException 或错误片段。

**本模块刻意不提供"自助注册"**：这是 B 端产品，账号由管理员发放。
开放注册的话，任何能访问到页面的人都能拿到问答权限。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ROLE_ADMIN, ROLE_USER, User
from app.security import hash_password


class UserError(Exception):
    """业务规则不满足。路由层翻译成 400/404/409 或错误片段。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def list_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).order_by(User.id)))


def create_user(db: Session, username: str, password: str, role: str = ROLE_USER) -> User:
    username = (username or "").strip()
    if not username:
        raise UserError("用户名不能为空")
    if role not in (ROLE_ADMIN, ROLE_USER):
        raise UserError(f"角色只能是 {ROLE_ADMIN} 或 {ROLE_USER}")

    exists = db.scalars(select(User).where(User.username == username)).first()
    if exists:
        raise UserError(f"用户名「{username}」已被占用", status_code=409)

    try:
        pw_hash = hash_password(password)
    except ValueError as exc:      # 密码超过 bcrypt 的 72 字节上限
        raise UserError(str(exc)) from exc

    user = User(username=username, password_hash=pw_hash, role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def reset_password(db: Session, user_id: int, password: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise UserError("用户不存在", status_code=404)
    try:
        user.password_hash = hash_password(password)
    except ValueError as exc:
        raise UserError(str(exc)) from exc
    db.commit()
    db.refresh(user)
    return user


def delete_user(db: Session, user_id: int, acting: User) -> User:
    """删除用户。有两道保护：

    1. **不能删自己** —— 否则管理员手一滑就把自己锁在门外了。
       这一条顺带就保护了"最后一个管理员"：单人管理员想删自己会被它拦住。

    2. **不能删掉最后一个管理员** —— 看着和上面重复，其实在 API 路径下
       **永远不会触发**（因为 acting 自己必然是管理员，且已被第 1 条排除，
       所以"除 target 外还有管理员"恒成立）。留着是纵深防御：
       `delete_user` 将来若被脚本或后台任务以别的身份调用，这条能兜住。
       真正的最后管理员保护在 `tools/adduser.py --delete` 里，那条是可达的。

    这两条都是"删得掉但后果不可逆"的操作，必须在代码里拦，不能靠提示。
    """
    user = db.get(User, user_id)
    if user is None:
        raise UserError("用户不存在", status_code=404)
    if user.id == acting.id:
        raise UserError("不能删除当前登录的自己")
    if user.role == ROLE_ADMIN:
        other_admins = db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.role == ROLE_ADMIN, User.id != user.id)
        )
        if not other_admins:
            raise UserError("这是最后一个管理员，删掉就没人能进管理端了")

    db.delete(user)
    db.commit()
    return user
