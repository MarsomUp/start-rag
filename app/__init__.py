"""start-rag 应用包。

这里只做一件事：在**任何 HuggingFace 相关模块被导入之前**把环境变量设好。

为什么必须放在包的 __init__ 里：huggingface_hub 的 HF_ENDPOINT / HF_HOME / HF_HUB_CACHE
都是「模块加载时读一次」的常量（见 huggingface_hub/constants.py），一旦 transformers
或 sentence-transformers 被导入就固定了，之后再改环境变量无效。
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 本机配了系统代理 127.0.0.1:10090 但代理客户端没启动，httpx 会走它然后被拒绝
# （WinError 10061）。统一绕开：实测直连能通 pypi / api.deepseek.com / hf-mirror.com。
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

# huggingface.co 直连不通（TLS 证书吊销检查失败），走国内镜像。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

# 模型权重缓存位置。默认在 C 盘（~/.cache/huggingface）容易占满，
# 本机已经把缓存迁到 D 盘并设了永久用户环境变量；这行是给还没重启的终端兜底的。
os.environ.setdefault("HF_HOME", r"D:\AI\HF-Home")

# 向量缓存目录：指向项目根目录的 .rag_cache，保持与已有缓存一致，
# 避免已经向量化过的书被重新计算（bge-small 跑一遍西游记约 3 分钟）。
os.environ.setdefault("RAG_CACHE_DIR", str(PROJECT_ROOT / ".rag_cache"))

__all__ = ["PROJECT_ROOT"]
