"""命令行创建/重置用户（服务没启动时也能用）。

用法：
    python tools/adduser.py                          列出所有用户
    python tools/adduser.py <用户名> <密码> [角色]    创建（角色默认 user）
    python tools/adduser.py <用户名> --reset <新密码> 改密码
    python tools/adduser.py <用户名> --delete         删除
    python tools/adduser.py <用户名> --role admin     改角色

什么时候用得上：
    - 忘了管理员密码，进不去「用户管理」页面
    - 想批量建账号（脚本里循环调用）
    - serviceless：不希望为了建个号先把服务跑起来

⚠️ 服务正在运行时也可以执行 —— SQLite 开了 WAL，支持一边写一边读。
   但别和大量导入同时跑，会争锁。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import ROLE_ADMIN, ROLE_USER, User  # noqa: E402
from app.security import hash_password, verify_password  # noqa: E402


def show_all(db) -> None:
    users = list(db.scalars(select(User).order_by(User.id)))
    if not users:
        print("还没有任何用户。启动一次服务会自动创建管理员，或用这个脚本手动建。")
        return
    print(f"{'ID':<5}{'用户名':<20}{'角色':<10}创建时间")
    print("-" * 60)
    for u in users:
        role = "管理员" if u.role == ROLE_ADMIN else "普通用户"
        print(f"{u.id:<5}{u.username:<20}{role:<10}{u.created_at:%Y-%m-%d %H:%M}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="命令行管理用户",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("username", nargs="?", help="用户名；不给则只列出所有用户")
    ap.add_argument("password", nargs="?", help="密码（创建时必填）")
    ap.add_argument("--role", choices=[ROLE_ADMIN, ROLE_USER], default=ROLE_USER)
    ap.add_argument("--reset", metavar="新密码", help="改密码")
    ap.add_argument("--delete", action="store_true", help="删除该用户")
    args = ap.parse_args()

    init_db()
    with SessionLocal() as db:
        if not args.username:
            show_all(db)
            return

        user = db.scalars(select(User).where(User.username == args.username)).first()

        # ---------- 删除 ----------
        if args.delete:
            if user is None:
                sys.exit(f"用户不存在：{args.username}")
            if user.role == ROLE_ADMIN:
                admins = list(db.scalars(select(User).where(User.role == ROLE_ADMIN)))
                if len(admins) <= 1:
                    sys.exit("这是最后一个管理员，删掉就没人能进管理端了。先建一个新的管理员。")
            db.delete(user)
            db.commit()
            print(f"已删除用户：{args.username}")
            return

        # ---------- 改密码 ----------
        if args.reset:
            if user is None:
                sys.exit(f"用户不存在：{args.username}")
            user.password_hash = hash_password(args.reset)
            db.commit()
            print(f"已重置 {args.username} 的密码。")
            return

        # ---------- 已存在：改角色 / 提示 ----------
        if user is not None:
            if args.role != user.role:
                if user.role == ROLE_ADMIN and args.role == ROLE_USER:
                    admins = list(db.scalars(select(User).where(User.role == ROLE_ADMIN)))
                    if len(admins) <= 1:
                        sys.exit("这是最后一个管理员，不能降级。先建一个新的管理员。")
                user.role = args.role
                db.commit()
                print(f"已把 {args.username} 的角色改成 {args.role}。")
            else:
                print(f"用户已存在：{args.username}（角色 {user.role}）")
                print(f"  改密码用：python tools/adduser.py {args.username} --reset <新密码>")
            return

        # ---------- 创建 ----------
        if not args.password:
            sys.exit("创建用户需要密码：python tools/adduser.py <用户名> <密码> [--role admin]")
        try:
            pw_hash = hash_password(args.password)
        except ValueError as exc:
            sys.exit(str(exc))

        db.add(User(username=args.username, password_hash=pw_hash, role=args.role))
        db.commit()
        print(f"已创建用户：{args.username}（角色 {args.role}）")


if __name__ == "__main__":
    main()
