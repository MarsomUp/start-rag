"""向量模型单例。

反面教材（服务化之后很容易犯）：
    def ask(q):
        emb = HuggingFaceEmbeddings(model_name=...)   # 每个请求都重新加载权重！
        ...

本机 bge-small 权重 183MB，反复加载会拖慢每一个请求。
所以这里做进程内单例，并且用锁保护，避免 API 线程和 Worker 线程同时初始化。
"""
from __future__ import annotations

import threading

from langchain_community.embeddings import HuggingFaceEmbeddings

from app.config import settings

_lock = threading.Lock()
_embeddings: HuggingFaceEmbeddings | None = None


def get_embeddings() -> HuggingFaceEmbeddings:
    """返回全局唯一的 embeddings 实例（双重检查加锁）。"""
    global _embeddings
    if _embeddings is None:
        with _lock:
            if _embeddings is None:
                _embeddings = HuggingFaceEmbeddings(
                    model_name=settings.model_name,
                    model_kwargs={"device": settings.device},
                    encode_kwargs={"normalize_embeddings": True},
                )
    return _embeddings
