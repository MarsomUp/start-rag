"""命令行版问答入口。

保留它是为了做回归测试 —— 改造代码之后，用命令还能问答说明核心链路没坏。

    python main.py

Web 版入口在 app/main.py：

    python -m uvicorn app.main:app --reload
"""
from __future__ import annotations

import os
from getpass import getpass

# 必须先 import app —— app/__init__.py 会在任何 HuggingFace 相关模块被导入之前
# 把 HF_ENDPOINT / HF_HOME / RAG_CACHE_DIR 设好，晚一步就不生效了。
from app.config import settings  # noqa: E402
from app.services.rag import get_engine  # noqa: E402

# 要换书就改这里
SOURCE_FILES = ["xiyouji.txt"]
TOP_K = 5
SHOW_SOURCES = True


def main() -> None:
    # API Key 必须在 get_engine() 之前准备好，因为引擎构造时就会初始化 LLM 客户端
    if not (settings.deepseek_api_key or os.environ.get("DEEPSEEK_API_KEY")):
        os.environ["DEEPSEEK_API_KEY"] = getpass("输入 Deepseek API Key: ")

    engine = get_engine()
    _, info = engine.get_store(SOURCE_FILES)   # 命中缓存时是毫秒级

    print("=" * 70)
    print("知识库问答（命令行版）")
    print(f"  语料      : {', '.join(SOURCE_FILES)}")
    print(f"  文本块    : {info['n_chunks']} 个（维度 {info['dim']}）")
    print(f"  向量模型  : {settings.model_name}")
    print(f"  检索      : k={TOP_K}    缓存：{info['status']}")
    print("  输入问题后回车；q / quit / exit 退出；/s 切换引用显示")
    print("=" * 70)

    show_sources = SHOW_SOURCES
    while True:
        try:
            question = input("\n问题> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            return

        if not question:
            continue
        if question.lower() in {"q", "quit", "exit", "退出"}:
            print("再见！")
            return
        if question == "/s":
            show_sources = not show_sources
            print(f"[引用片段显示：{'开' if show_sources else '关'}]")
            continue

        try:
            result = engine.ask(question, SOURCE_FILES, k=TOP_K,
                                with_sources=show_sources,
                                book_names=SOURCE_FILES)
        except Exception as exc:
            # 网络/鉴权等错误不应该让整个程序退出
            print(f"[调用失败] {type(exc).__name__}: {exc}")
            continue

        if show_sources:
            print(f"\n引用片段（{len(result.sources)} 条，按相关度排序）：")
            for s in result.sources:
                print(f"  {s.index}. [{s.book}] {s.snippet}")

        print(f"\n回答> {result.text}")


if __name__ == "__main__":
    main()
