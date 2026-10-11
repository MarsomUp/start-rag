"""RAG 引擎：检索 + 生成。

CLI（根目录 main.py）和 Web（app/routers/chat.py）共用这一份实现，避免逻辑分叉。
LLM 是延迟构造的 —— 这样不带 DEEPSEEK_API_KEY 也能启动 Web 服务做上传等操作。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from app.config import settings
from app.rag_cache import build_or_load
from app.services.embeddings import get_embeddings

PROMPT_TEMPLATE = """你是一个知识库助手。请仅基于以下上下文回答问题，不要使用上下文之外的知识。

当前知识库包含的资料：《{books}》

如果上下文里确实没有回答所需的信息，请按两步回答：
1. 先明确说“对不起，无法从当前知识库搜索到该问题答案。”；
2. 再用一句话说明：检索到的资料实际在讲什么，以及知识库里都有哪些资料 ——
   这样提问者能判断到底是知识库里没有，还是自己的问法不对。

上下文：
{context}

问题：{question}
"""


def format_docs(docs: Sequence[Document]) -> str:
    return "\n\n".join(d.page_content for d in docs)


@dataclass
class Source:
    """一条引用来源，给前端展示"答案是从哪来的"。"""

    index: int
    book: str | None
    snippet: str


@dataclass
class Answer:
    question: str
    text: str
    sources: list[Source] = field(default_factory=list)
    elapsed: float = 0.0
    cache_status: str = ""


class RagEngine:
    """检索 + 生成。一个进程里共用一个实例。"""

    def __init__(self) -> None:
        # 延迟到真正需要时才导入 LLM 客户端
        from langchain_deepseek import ChatDeepSeek

        self.embeddings = get_embeddings()
        self.llm = ChatDeepSeek(
            model=settings.deepseek_model,
            api_key=settings.deepseek_api_key or None,
            temperature=settings.llm_temperature,
            max_retries=settings.llm_max_retries,
        )
        self.prompt = ChatPromptTemplate.from_template(PROMPT_TEMPLATE)
        self.chain = self.prompt | self.llm | StrOutputParser()

    # ------------------------------------------------------------------
    # 向量库
    # ------------------------------------------------------------------
    def get_store(self, files: Sequence[str | Path]):
        """按选中的书取（或构建）向量库。

        直接复用 rag_cache 的两级缓存：
          books/<book_key>/     每本书的向量，只算一次
          indexes/<组合 key>/   任意几本书组合成的索引
        所以"选中一批书"这件事本身不需要手写 fan-out 合并。
        """
        return build_or_load(
            list(files),
            embeddings=self.embeddings,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            batch_size=settings.batch_size,
            # 这是每个请求都会走的热路径。不关的话，每次提问都会往控制台
            # 刷一遍 "[hit] ... 缓存目录 ..." 的详情，把有用的日志淹掉。
            verbose=False,
        )

    # ------------------------------------------------------------------
    # 只检索（不调 LLM，便于调试检索质量）
    # ------------------------------------------------------------------
    def retrieve(self, question: str, files: Sequence[str | Path],
                 k: int | None = None) -> list[Document]:
        store, _ = self.get_store(files)
        return store.similarity_search(question, k=k or settings.top_k)

    # ------------------------------------------------------------------
    # 问答
    # ------------------------------------------------------------------
    def ask(self, question: str, files: Sequence[str | Path],
            k: int | None = None, with_sources: bool = True,
            book_names: Sequence[str] | None = None) -> Answer:
        t0 = time.time()
        store, info = self.get_store(files)
        docs = store.similarity_search(question, k=k or settings.top_k)

        # 把「知识库里到底有哪些资料」告诉模型。
        # 否则它只能说"我无法回答"；有了这个它就能说清
        # "知识库里只有《三国演义》，没有关于孙悟空的内容" —— 这才对提问者有用。
        names = "、".join(book_names) if book_names else "（未指定）"

        text = self.chain.invoke(
            {"context": format_docs(docs), "question": question, "books": names}
        )

        sources: list[Source] = []
        if with_sources:
            for i, d in enumerate(docs, 1):
                snippet = d.page_content.replace("\n", " ").strip()
                sources.append(
                    Source(
                        index=i,
                        book=d.metadata.get("book"),
                        snippet=snippet[:120] + ("..." if len(snippet) > 120 else ""),
                    )
                )

        return Answer(
            question=question,
            text=text,
            sources=sources,
            elapsed=time.time() - t0,
            cache_status=info.get("status", ""),
        )


# ----------------------------------------------------------------------
# 全局单例
# ----------------------------------------------------------------------
_engine: RagEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> RagEngine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = RagEngine()
    return _engine
