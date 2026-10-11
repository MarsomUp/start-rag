"""BM25 从零实现 + 与现成库对照 + 中文分词方案对比。

运行：
    python examples/bm25_demo.py

不依赖向量模型，直接读 .rag_cache 里缓存好的文本块，几秒就能跑完。
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

import jieba

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".rag_cache" / "books" / "a3892502c19aa4ec" / "texts.json"


# ======================================================================
# 一、BM25 从零实现
# ======================================================================
class BM25:
    """标准 BM25（Okapi）。只有 4 个概念：TF、IDF、长度归一化、饱和。

    公式（对查询里的每个词求和）：

                     TF(t,d) · (k1+1)
        IDF(t) · ──────────────────────────────────────
                  TF(t,d) + k1·(1 − b + b·|d|/avgdl)
    """

    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1          # 控制"饱和"：词频加到多少就不再明显加分
        self.b = b            # 控制"长度归一化"：0=不归一化，1=完全归一化
        self.docs = corpus
        self.n = len(corpus)
        self.avgdl = sum(len(d) for d in corpus) / max(self.n, 1)

        # 每篇文档的词频表：{"曹操": 3, "孟德": 1, ...}
        self.tfs = [Counter(d) for d in corpus]

        # 文档频率 df：某个词出现在多少篇文档里
        df: Counter[str] = Counter()
        for tf in self.tfs:
            df.update(tf.keys())

        # IDF：词越稀有，权重越高。
        # 这里用 log(1 + ...) 而不是 log((N-f+0.5)/(f+0.5))，
        # 后者在词出现在半数以上文档时会变成负数（Lucene 也用前者）。
        self.idf = {
            t: math.log(1 + (self.n - f + 0.5) / (f + 0.5))
            for t, f in df.items()
        }

    def score(self, query: list[str], i: int) -> float:
        """给第 i 篇文档打分。"""
        dl = len(self.docs[i])
        norm = 1 - self.b + self.b * dl / self.avgdl   # 长度归一化因子
        total = 0.0
        for t in query:
            idf = self.idf.get(t)
            if idf is None:            # 语料里没见过这个词
                continue
            f = self.tfs[i].get(t, 0)
            if f == 0:                 # 这篇文档里没有这个词
                continue
            total += idf * f * (self.k1 + 1) / (f + self.k1 * norm)
        return total

    def rank(self, query: list[str]) -> list[tuple[int, float]]:
        """返回 [(文档下标, 分数), ...]，按分数从高到低。"""
        scored = [(i, self.score(query, i)) for i in range(self.n)]
        return sorted(scored, key=lambda x: -x[1])


# ======================================================================
# 二、中文分词：BM25 对中文，分词方式的选择比参数更重要
# ======================================================================
_PUNCT = re.compile(r"^[\s\W_]+$")


def tok_jieba(text: str) -> list[str]:
    """jieba 精确模式。需要 pip install jieba。"""
    return [w for w in jieba.lcut(text) if w.strip() and not _PUNCT.match(w)]


def tok_bigram(text: str) -> list[str]:
    """字符二元组。不需要任何依赖，对中文意外地有效。"""
    s = re.sub(r"\s+", "", text)
    return [s[i:i + 2] for i in range(len(s) - 1)]


def tok_char(text: str) -> list[str]:
    """单字。最粗暴，但召回最全。"""
    return [c for c in text if not _PUNCT.match(c)]


# ======================================================================
# 三、跑起来
# ======================================================================
def main() -> None:
    texts = json.loads(CACHE.read_text(encoding="utf-8"))
    corpus = [t["page_content"] for t in texts]
    print(f"语料：{len(corpus)} 个文本块\n")

    # 找"正确答案"那一块（曹操的自我介绍：姓曹名操字孟德）
    target = next(i for i, c in enumerate(corpus) if "姓曹名操字孟德" in c)
    print("目标块（含『姓曹名操字孟德』）索引 =", target)
    print("  ", corpus[target][:80].replace("\n", " "), "...\n")

    queries = ["曹操的别名是什么？", "阿瞒是谁？", "曹操 孟德 阿瞒"]

    # ---------- 分词方案对比 ----------
    print("=" * 66)
    print("分词方案对 BM25 排名的影响（数字 = 目标块的排名，越小越好）")
    print("=" * 66)
    print(f"{'查询':<22}{'jieba':>8}{'bigram':>8}{'单字':>8}")
    for q in queries:
        row = []
        for fn in (tok_jieba, tok_bigram, tok_char):
            bm = BM25([fn(c) for c in corpus])
            order = [i for i, _ in bm.rank(fn(q))]
            row.append(order.index(target) + 1)
        print(f"{q:<22}{row[0]:>8}{row[1]:>8}{row[2]:>8}")

    # ---------- 和现成库对照 ----------
    print()
    print("=" * 66)
    print("和 rank_bm25 对照（验证自己写对了没）")
    print("=" * 66)
    try:
        from rank_bm25 import BM25Okapi

        docs = [tok_jieba(c) for c in corpus]
        mine = BM25(docs)
        lib = BM25Okapi(docs)

        for q in queries:
            qt = tok_jieba(q)
            my_order = [i for i, _ in mine.rank(qt)]
            lib_scores = lib.get_scores(qt)
            lib_order = sorted(range(len(lib_scores)), key=lambda i: -lib_scores[i])

            my_rank = my_order.index(target) + 1
            lib_rank = lib_order.index(target) + 1
            # 前 10 名重合几个
            overlap = len(set(my_order[:10]) & set(lib_order[:10]))
            print(f"{q:<22} 我的排名={my_rank:<5} 库的排名={lib_rank:<5} 前10重合={overlap}/10")
    except ImportError:
        print("  （没装 rank_bm25，跳过）")


if __name__ == "__main__":
    main()
