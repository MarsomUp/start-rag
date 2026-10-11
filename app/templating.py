"""共享的 Jinja2 模板对象。

单独放一个模块是为了避免循环导入：
`app.main` 要导入路由，而路由也要用 templates。
"""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

WEB_DIR = Path(__file__).resolve().parent / "web"

templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
