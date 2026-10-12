"""认证与授权。

三个设计决定，都写在前面，方便以后回看"为什么这么做"：

**① 会话用「签名 cookie」，不引入 Redis 或会话表。**
   `starlette.middleware.sessions.SessionMiddleware` 把会话内容 base64 编码后用
   HMAC 签名放进 cookie，服务端不存任何东西。内容只有 `{"uid": 1}`。
   ⚠️ 刻意**不把 role 放进 cookie** —— 那样改了用户角色要等重新登录才生效。
   这里每次请求都用 uid 现查数据库，所以"把某人降级成普通用户"立刻生效。

**② 密码用 bcrypt，不用 sha256/md5。**
   sha256 快得正是缺点：显卡每秒能试几十亿次，密码库被拖走就等于明文。
   bcrypt 故意设计得慢（自带工作因子），而且每次哈希都带随机盐 ——
   同一个密码哈希两次结果不同，所以不能用 `==` 比，必须用 `checkpw`。

**③ 未登录时的响应要按请求类型分三种**（见 `handle_auth_required`）：
   - htmx 请求 → 回 `HX-Redirect` 响应头
   - 浏览器页面 → 303 跳转 /login?next=...
   - API/脚本 → 401 JSON
   直接对所有请求都 302 是不行的：htmx 会把登录页当成"响应内容"swap 进目标元素，
   界面上会出现一块莫名其妙的登录表单。
"""
from __future__ import annotations

import secrets
from pathlib import Path
from urllib.parse import urlparse

import bcrypt
from fastapi import Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import ROLE_ADMIN, ROLE_USER, User

BCRYPT_MAX_BYTES = 72
"""bcrypt 只处理前 72 字节。超了会报错，所以提前拦下来给个清楚的提示
（中文一个字 3 字节，72 字节 ≈ 24 个汉字）。"""


# ----------------------------------------------------------------------
# 异常：由路由层抛出，main.py 里注册的处理器决定返回什么
# ----------------------------------------------------------------------
class AuthRequired(Exception):
    """没登录。"""


class PermissionDenied(Exception):
    """登录了，但权限不够（比如普通用户想进管理端）。"""


# ----------------------------------------------------------------------
# 密码
# ----------------------------------------------------------------------
def hash_password(password: str) -> str:
    raw = password.encode("utf-8")
    if len(raw) > BCRYPT_MAX_BYTES:
        raise ValueError(f"密码过长：bcrypt 上限 {BCRYPT_MAX_BYTES} 字节（约 24 个汉字）")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError, AttributeError):
        # 哈希串格式不对（比如数据库被手改过）——当成验证失败，不要抛 500
        return False


# ----------------------------------------------------------------------
# 会话密钥
# ----------------------------------------------------------------------
_secret_cache: str | None = None


def get_session_secret() -> str:
    """拿到签名密钥：优先环境变量，否则自动生成并落盘。

    必须**跨重启保持稳定**，否则每次重启都把所有用户登出。
    所以自动生成的要存到 data/.session_secret（data/ 已在 .gitignore 里）。
    """
    global _secret_cache
    if _secret_cache:
        return _secret_cache

    if settings.rag_session_secret:
        _secret_cache = settings.rag_session_secret
        return _secret_cache

    path = Path(settings.data_dir) / ".session_secret"
    if path.exists():
        _secret_cache = path.read_text(encoding="utf-8").strip()
    else:
        _secret_cache = secrets.token_urlsafe(48)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_secret_cache, encoding="utf-8")
    return _secret_cache


# ----------------------------------------------------------------------
# 会话读写
# ----------------------------------------------------------------------
def login_user(request: Request, user: User) -> None:
    request.session["uid"] = user.id


def logout_user(request: Request) -> None:
    request.session.clear()


def get_current_user(request: Request, db: Session) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    user = db.get(User, uid)
    if user is None:
        # 用户被删了但 cookie 还在 —— 清掉会话，免得每请求都查一次空
        request.session.clear()
    return user


# ----------------------------------------------------------------------
# FastAPI 依赖
# ----------------------------------------------------------------------
def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    """任何已登录用户（含管理员）。"""
    user = get_current_user(request, db)
    if user is None:
        raise AuthRequired()
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    """仅管理员。"""
    user = get_current_user(request, db)
    if user is None:
        raise AuthRequired()
    if user.role != ROLE_ADMIN:
        raise PermissionDenied()
    return user


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """用于落地页这类"登录与否都能看"的页面，只为了在导航栏显示用户名。"""
    return get_current_user(request, db)


def page_context(request: Request, db: Session, **extra) -> dict:
    """所有页面模板都需要的公共上下文。

    每个页面路由都调它，把 `current_user` 注进去 —— base.html 靠它决定
    导航栏显示什么（未登录就只显示品牌，不显示进不去的入口）。
    """
    return {"current_user": get_current_user(request, db), **extra}


# ----------------------------------------------------------------------
# 请求类型判断 + 响应构造（给 main.py 的异常处理器用）
# ----------------------------------------------------------------------
def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def wants_html(request: Request) -> bool:
    """这个请求想要 HTML 页面，还是 JSON？

    **判据刻意用路径而不是 `Accept` 头。**

    最初写的是 `"text/html" in Accept`，看起来合理 —— 浏览器导航确实会带它。
    但实测发现 TestClient、curl、以及不少脚本和代理发的是 `Accept: */*`，
    于是同一个未登录请求：浏览器拿到 303 跳登录页，脚本却拿到 401 JSON。
    行为随客户端而变，很难排查。

    改成看路径就很确定：
        `/api/*`  是给程序调用的  → JSON
        其余       都是页面        → HTML（303 跳登录页）

    htmx 片段请求要在**调用本函数之前**先用 `is_htmx` 拦掉，它走 HX-Redirect。
    """
    return not request.url.path.startswith("/api/")


def safe_next(target: str | None) -> str:
    """登录后跳回哪里。只允许站内相对路径，防开放重定向。

    必须挡住 `//evil.com` —— 它以 `/` 开头，但浏览器会当成
    「协议相对的绝对地址」，于是跳到外站去。
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def login_redirect_url(request: Request) -> str:
    """未登录时该跳到哪 —— 带上"登录后回到哪里"。

    htmx 片段请求要特别处理：这时 `request.url` 是**片段地址**
    （比如 `/admin/partials/books`），拿它当 next 的话，
    登录成功后你会看到一个光秃秃的 HTML 片段，而不是完整页面。

    所以 htmx 请求优先用它自己发的 `HX-Current-URL`（当前所在**页面**的完整地址）。
    """
    if is_htmx(request):
        current = request.headers.get("HX-Current-URL")
        if current:
            parsed = urlparse(current)
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            return f"/login?next={target}"

    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    return f"/login?next={target}"


# ----------------------------------------------------------------------
# 首次启动引导管理员
# ----------------------------------------------------------------------
def ensure_admin_user(db: Session) -> tuple[User | None, str | None]:
    """users 表为空时创建一个管理员。

    Returns:
        (创建的用户 或 None, 需要打印给用户看的明文密码 或 None)
        第二个值只有在「密码是随机生成的」时候才非空 —— 配置里写死了密码就不打印。
    """
    if db.scalar(select(func.count()).select_from(User)):
        return None, None

    username = settings.rag_admin_username
    generated = not settings.rag_admin_password
    password = settings.rag_admin_password or secrets.token_urlsafe(12)

    user = User(
        username=username,
        password_hash=hash_password(password),
        role=ROLE_ADMIN,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user, (password if generated else None)


__all__ = [
    "AuthRequired",
    "PermissionDenied",
    "ROLE_ADMIN",
    "ROLE_USER",
    "ensure_admin_user",
    "get_current_user",
    "get_session_secret",
    "hash_password",
    "is_htmx",
    "login_redirect_url",
    "login_user",
    "logout_user",
    "optional_user",
    "page_context",
    "require_admin",
    "require_user",
    "safe_next",
    "verify_password",
    "wants_html",
]
