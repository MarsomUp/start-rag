"""向量库本地缓存：判断"是否已经向量化过"，并复用磁盘上的结果。

为什么要这样设计
----------------
向量化（embedding）是这个项目里最慢的一步：i7-8565U 上 bge-large 大约
每秒 1 个块，《西游记》1565 个块要 30 分钟以上。而检索本身是毫秒级的。
所以工程上的标准做法是：**算一次，存磁盘，之后直接加载。**

缓存键（指纹）必须覆盖所有会影响向量结果的输入，否则会出现"文件改了、
却还在用旧向量"这种不报错的错误：

    文件内容 sha256  +  chunk_size  +  chunk_overlap  +  separators
    +  向量模型名  +  是否归一化

任何一项变化 -> 指纹变化 -> 自动重新向量化。文件路径故意不参与指纹，
所以同一个文件改名不会重建（代价只是缓存目录名不同）。

两级缓存
--------
1. books/<book_key>/      —— 单个文件的**向量**（vectors.npy + texts.json）
                             每个文件只向量化一次，永不复算
2. indexes/<index_key>/   —— 任意几个文件组合成的 FAISS 索引
                             index_key 由 book_key 列表决定

好处：这次只读 xiyouji.txt，下次加 shuihu.txt 时**只向量化 shuihu**，
西游记的 1565 个块直接从磁盘读回，不重新算。

用法
----
    from rag_cache import build_or_load, inspect_cache

    vs, info = build_or_load(["xiyouji.txt"])          # 第一次：慢，之后：秒开
    retriever = vs.as_retriever(search_kwargs={"k": 5})

    vs, info = build_or_load(["xiyouji.txt", "shuihu.txt"])   # 多本书合成一个库

    # 只要西游记的内容，用 metadata 过滤（每块都打了 book 标签）
    retriever = vs.as_retriever(
        search_kwargs={"k": 5, "filter": {"book": "xiyouji"}}
    )

    inspect_cache()        # 打印本地已经向量化了哪些东西
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from langchain_community.document_loaders import TextLoader
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_community.vectorstores.utils import DistanceStrategy
from langchain_text_splitters import RecursiveCharacterTextSplitter

DEFAULT_MODEL = "BAAI/bge-large-zh-v1.5"
DEFAULT_SEPARATORS = ("\n\n", "\n", "。", "！", "？", " ", "")

# 进度回调：(已完成的块数, 总块数) -> None
# 给后台 Worker 用来把进度写进数据库，前端再轮询展示。
ProgressCb = Callable[[int, int], None]

# 缓存根目录，可用环境变量 RAG_CACHE_DIR 覆盖（方便测试）
CACHE_ROOT = Path(
    os.environ.get("RAG_CACHE_DIR", Path(__file__).resolve().parent / ".rag_cache")
)


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def sha256_file(path: str | Path, block: int = 1 << 20) -> str:
    """文件内容哈希。2MB 的西游记瞬间算完，比向量化快几万倍。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(block), b""):
            h.update(chunk)
    return h.hexdigest()


def make_embeddings(
    model_name: str = DEFAULT_MODEL,
    normalize: bool = True,
    device: str = "cpu",
) -> HuggingFaceEmbeddings:
    return HuggingFaceEmbeddings(
        model_name=model_name,
        model_kwargs={"device": device},
        encode_kwargs={"normalize_embeddings": normalize},
    )


def _split(path: str | Path, chunk_size: int, chunk_overlap: int,
           separators: Sequence[str]) -> list:
    """加载 + 切分，并给每块打上 book 标签（用于多本书检索时过滤）。"""
    path = Path(path)
    docs = TextLoader(str(path), encoding="utf-8").load()
    splits = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=list(separators),
    ).split_documents(docs)
    for d in splits:
        d.metadata["book"] = path.stem
    return splits


def _book_key(file_sha: str, chunk_size: int, chunk_overlap: int,
              separators: Sequence[str], model_name: str,
              normalize: bool) -> str:
    payload = {
        "sha256": file_sha,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "separators": list(separators),
        "model": model_name,
        "normalize": normalize,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _index_key(book_keys: Iterable[str], model_name: str, normalize: bool) -> str:
    payload = {
        "books": sorted(book_keys),
        "model": model_name,
        "normalize": normalize,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# --------------------------------------------------------------------------
# 第一级：单文件向量缓存
# --------------------------------------------------------------------------
def _embed_batched(
    embeddings,
    texts: list[str],
    batch_size: int,
    bdir: Path,
    verbose: bool = True,
    on_progress: ProgressCb | None = None,
) -> np.ndarray:
    """分批向量化，带进度/ETA，并把中间结果写成断点。

    为什么要分批：bge-large 在这台 4 核低压 CPU 上，1565 个块要跑 1~3 小时。
    一次性调用 embed_documents 期间**一行输出都没有**，看起来就像卡死了；
    而且算完才落盘，中途 Ctrl+C 就全部白费。
    分批 + 断点解决了这两个问题。
    """
    n = len(texts)
    ckpt_vec = bdir / "checkpoint_vectors.npy"
    ckpt_meta = bdir / "checkpoint.json"
    bdir.mkdir(parents=True, exist_ok=True)

    # ---- 读断点：上次算到哪了 ----
    vectors, done = None, 0
    if ckpt_vec.exists() and ckpt_meta.exists():
        try:
            info = _read_json(ckpt_meta) or {}
            if info.get("n_total") == n:
                v = np.load(ckpt_vec)
                if v.shape[0] == int(info.get("done", -1)):
                    vectors, done = v, int(info["done"])
        except Exception:
            vectors, done = None, 0

    resumed = done
    if done and verbose:
        print(f"    发现断点：从 {done}/{n} 继续，之前的计算不会白费", flush=True)

    # 先报一次 (已完成, 总数)。
    # 否则界面在"加载模型 + 切分 + 跑完第一批"这段时间里只能显示 0/0，
    # 看起来像卡死（实测第一批之前要等 30 秒以上）。
    if on_progress is not None:
        try:
            on_progress(done, n)
        except Exception:
            pass

    t0 = time.time()
    for start in range(done, n, batch_size):
        batch = texts[start:start + batch_size]
        emb = np.asarray(embeddings.embed_documents(batch), dtype="float32")
        vectors = emb if vectors is None else np.vstack([vectors, emb])
        done += len(batch)

        # 每批都把当前结果落盘，这样随时中断都能续
        np.save(ckpt_vec, vectors)
        ckpt_meta.write_text(
            json.dumps({"done": done, "n_total": n}), encoding="utf-8"
        )

        # 上报进度（后台 Worker 用它更新数据库里的 jobs.progress）
        if on_progress is not None:
            try:
                on_progress(done, n)
            except Exception:
                pass  # 进度上报失败不能影响向量化本身

        if verbose:
            elapsed = time.time() - t0
            new_done = done - resumed
            if new_done > 0 and elapsed > 0:
                rate = new_done / elapsed
                eta = (n - done) / rate if rate > 0 else 0.0
                print(
                    f"    {done:>5}/{n} ({done / n * 100:5.1f}%)  "
                    f"本次已用 {elapsed / 60:5.1f} 分钟  "
                    f"预计还需 {eta / 60:5.1f} 分钟",
                    flush=True,
                )
    return vectors


def _ensure_book_vectors(
    path: Path,
    book_key: str,
    file_sha: str,
    embeddings,
    chunk_size: int,
    chunk_overlap: int,
    separators: Sequence[str],
    batch_size: int = 32,
    verbose: bool = True,
    on_progress: ProgressCb | None = None,
) -> dict:
    """返回 {"key","texts","metadatas","vectors","status","chunks"}。

    status = "reused"（磁盘命中，没跑模型） 或 "embedded"（真的算了）
    """
    bdir = CACHE_ROOT / "books" / book_key
    vec_file = bdir / "vectors.npy"
    txt_file = bdir / "texts.json"
    meta_file = bdir / "meta.json"

    # ---- 命中检查：三个文件都在，且 meta 里的块数与实际一致 ----
    if vec_file.exists() and txt_file.exists() and meta_file.exists():
        meta = _read_json(meta_file)
        texts = _read_json(txt_file)
        if meta and texts and len(texts) == meta.get("n_chunks"):
            try:
                vectors = np.load(vec_file)
                if vectors.shape[0] == len(texts):
                    return {
                        "key": book_key,
                        "texts": [t["page_content"] for t in texts],
                        "metadatas": [t["metadata"] for t in texts],
                        "vectors": vectors,
                        "status": "reused",
                        "chunks": len(texts),
                        "model": meta.get("model"),
                    }
            except Exception:
                pass  # 文件损坏 -> 落到下面重建

    # ---- 未命中：切分 + 分批向量化（带进度、ETA、断点续算）----
    if verbose:
        print(f"    正在切分 {path.name} ...", flush=True)
    splits = _split(path, chunk_size, chunk_overlap, separators)
    texts = [d.page_content for d in splits]
    metadatas = [d.metadata for d in splits]
    if verbose:
        print(
            f"    切分为 {len(texts)} 个块，开始向量化"
            f"（batch_size={batch_size}；中途 Ctrl+C，下次会自动续算）",
            flush=True,
        )

    t0 = time.time()
    vectors = _embed_batched(
        embeddings, texts, batch_size, bdir, verbose=verbose, on_progress=on_progress
    )
    elapsed = time.time() - t0

    bdir.mkdir(parents=True, exist_ok=True)
    np.save(vec_file, vectors)
    # 最终文件已经写好，断点没用了，清掉
    for stale in ("checkpoint_vectors.npy", "checkpoint.json"):
        (bdir / stale).unlink(missing_ok=True)
    txt_file.write_text(
        json.dumps(
            [{"page_content": t, "metadata": m} for t, m in zip(texts, metadatas)],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    meta_file.write_text(
        json.dumps(
            {
                "source": str(path),
                "file": path.name,
                "book": path.stem,
                "sha256": file_sha,
                "n_chunks": len(texts),
                "dim": int(vectors.shape[1]),
                "model": embeddings.model_name,
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
                "separators": list(separators),
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "key": book_key,
        "texts": texts,
        "metadatas": metadatas,
        "vectors": vectors,
        "status": "embedded",
        "chunks": len(texts),
        "seconds": round(elapsed, 1),
        "model": embeddings.model_name,
    }


# --------------------------------------------------------------------------
# 第二级：索引缓存 + 对外主接口
# --------------------------------------------------------------------------
def build_or_load(
    paths: str | Path | Sequence[str | Path],
    *,
    embeddings=None,
    model_name: str | None = None,
    normalize: bool = True,
    chunk_size: int = 600,
    chunk_overlap: int = 80,
    batch_size: int = 32,
    separators: Sequence[str] = DEFAULT_SEPARATORS,
    device: str = "cpu",
    force: bool = False,
    verbose: bool = True,
    on_progress: ProgressCb | None = None,
) -> tuple[FAISS, dict]:
    """构建或加载向量库。命中缓存时**不切分、不跑模型**。

    关于 `model_name` / `normalize`
    -------------------------------
    这两个参数会**参与缓存键的计算**，所以它们必须准确反映
    "这些向量到底是用什么模型、什么方式算出来的"，否则键和向量会对不上。

    - `model_name=None`（推荐）：自动从传入的 `embeddings` 对象推断。
      传 `embeddings` 时不写 `model_name` 就行，不可能写错。
    - 显式传 `model_name`：必须和 `embeddings` 实际的模型一致，否则直接报错。
    - `normalize` 必须和 `embeddings` 的 `normalize_embeddings` 一致 ——
      它同时决定缓存键**和** FAISS 的距离度量
      （True→内积，False→欧氏距离）。不一致会用错度量方式。

    Returns
    -------
    (vectorstore, info)  info 里记录了每个文件是 reused 还是 embedded。
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    paths = [Path(p).resolve() for p in paths]
    for p in paths:
        if not p.exists():
            raise FileNotFoundError(f"找不到文件：{p}")

    # ---- 0. 先确定"到底用的是哪个向量模型" ----
    # ⚠️ 这里曾经有个隐蔽的 bug：签名默认 DEFAULT_MODEL（bge-large），
    # 而调用方传进来的 embeddings 实际是 bge-small（来自 settings.model_name）。
    # 于是缓存键记着 large、向量却是 small —— 平时一致所以看不出来，
    # 但一旦在 config 里换了模型，键**不会变**，就会命中旧向量，
    # 轻则维度不匹配报错，重则静默用错检索结果。
    actual_model = getattr(embeddings, "model_name", None) if embeddings is not None else None
    if model_name is None:
        model_name = actual_model or DEFAULT_MODEL
    elif actual_model and actual_model != model_name:
        raise ValueError(
            f"model_name={model_name!r} 与 embeddings 实际使用的模型 "
            f"{actual_model!r} 不一致。\n"
            f"这会让缓存键和向量内容对不上（换了模型仍命中旧缓存）。\n"
            f"修法：不要传 model_name，让它从 embeddings 自动推断。"
        )

    # ---- 1. 只用"文件哈希 + 参数"算指纹，这一步不需要切分 ----
    book_keys, file_shas = [], []
    for p in paths:
        s = sha256_file(p)
        file_shas.append(s)
        book_keys.append(
            _book_key(s, chunk_size, chunk_overlap, separators, model_name, normalize)
        )
    ikey = _index_key(book_keys, model_name, normalize)
    idir = CACHE_ROOT / "indexes" / ikey

    strategy = (
        DistanceStrategy.MAX_INNER_PRODUCT if normalize else DistanceStrategy.EUCLIDEAN_DISTANCE
    )

    if embeddings is None:
        embeddings = make_embeddings(model_name, normalize, device)

    info: dict[str, Any] = {"index_key": ikey, "index_dir": str(idir), "books": []}

    # ---- 2. 索引级命中：最快路径，直接加载，完全不碰文本 ----
    if not force and (idir / "index.faiss").exists() and (idir / "index.pkl").exists():
        meta = _read_json(idir / "meta.json") or {}
        try:
            vs = FAISS.load_local(
                str(idir),
                embeddings,
                allow_dangerous_deserialization=True,
                distance_strategy=strategy,
            )
            if vs.index.ntotal == meta.get("n_total"):
                # 命中走的是"完全不碰文本"的最快路径，但调用方（入库服务）需要
                # 知道每本书的块数，所以这里补读一次各书的 meta.json，
                # 保证 info 的结构与未命中路径完全一致。
                for p, bk in zip(paths, book_keys):
                    bmeta = _read_json(CACHE_ROOT / "books" / bk / "meta.json") or {}
                    info["books"].append(
                        {
                            "file": p.name,
                            "key": bk,
                            "status": "reused",
                            "chunks": int(bmeta.get("n_chunks") or 0),
                            "seconds": 0.0,
                        }
                    )
                info.update(
                    status="hit",
                    n_chunks=int(vs.index.ntotal),
                    dim=int(vs.index.d),
                    note="索引缓存命中，未切分、未运行向量模型",
                )
                if verbose:
                    _report(paths, info)
                return vs, info
        except Exception:
            pass  # 损坏 -> 重建

    # ---- 3. 未命中：逐本书取向量（每本书自己有缓存）----
    all_texts, all_metas, all_vecs = [], [], []
    for p, bk, s in zip(paths, book_keys, file_shas):
        got = _ensure_book_vectors(
            p, bk, s, embeddings, chunk_size, chunk_overlap, separators,
            batch_size=batch_size, verbose=verbose, on_progress=on_progress,
        )
        info["books"].append(
            {
                "file": p.name,
                "key": bk,
                "status": got["status"],
                "chunks": got["chunks"],
                "seconds": got.get("seconds", 0.0),
            }
        )
        all_texts.extend(got["texts"])
        all_metas.extend(got["metadatas"])
        all_vecs.append(got["vectors"])

    vectors = np.vstack(all_vecs)

    # ---- 4. 组装 FAISS 并落盘 ----
    vs = FAISS.from_embeddings(
        list(zip(all_texts, vectors)),
        embeddings,
        metadatas=all_metas,
        distance_strategy=strategy,
    )
    idir.mkdir(parents=True, exist_ok=True)
    vs.save_local(str(idir))
    (idir / "meta.json").write_text(
        json.dumps(
            {
                "books": [p.name for p in paths],
                "book_keys": book_keys,
                "file_shas": file_shas,
                "n_total": int(vs.index.ntotal),
                "dim": int(vs.index.d),
                "model": model_name,
                "normalize": normalize,
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
                "built": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    info.update(
        status="rebuilt",
        n_chunks=int(vs.index.ntotal),
        dim=int(vs.index.d),
        note="有文件需要向量化，已写入缓存",
    )
    if verbose:
        _report(paths, info)
    return vs, info


def _report(paths, info) -> None:
    print(f"[{info['status']}] {info['note']}")
    for b in info.get("books", []):
        if b["status"] == "reused":
            tag = "复用磁盘向量（没跑模型）"
        else:
            secs = b.get("seconds", 0.0)
            tag = (f"新向量化 {secs / 60:.1f} 分钟" if secs >= 60
                   else f"新向量化 {secs:.1f} 秒")
        print(f"    - {b['file']:<16} {b['chunks']:>5} 块   {tag}")
    print(f"    合计 {info['n_chunks']} 块, 维度 {info['dim']}")
    print(f"    缓存目录 {info['index_dir']}")


# --------------------------------------------------------------------------
# 观测：本地到底已经向量化了什么
# --------------------------------------------------------------------------
def inspect_cache(verbose: bool = True) -> dict:
    """扫描缓存目录，回答"本地是否已经有向量化"。"""
    books, indexes = [], []
    bdir = CACHE_ROOT / "books"
    if bdir.exists():
        for d in sorted(bdir.iterdir()):
            if not d.is_dir():
                continue
            m = _read_json(d / "meta.json")
            if not m:
                continue
            ok = (d / "vectors.npy").exists() and (d / "texts.json").exists()
            books.append(
                {
                    "key": d.name,
                    "file": m.get("file"),
                    "chunks": m.get("n_chunks"),
                    "dim": m.get("dim"),
                    "model": m.get("model"),
                    "chunk_size": m.get("chunk_size"),
                    "created": m.get("created"),
                    "size_mb": round(
                        sum(f.stat().st_size for f in d.iterdir() if f.is_file())
                        / 1024 / 1024,
                        2,
                    ),
                    "complete": ok,
                }
            )
    idir = CACHE_ROOT / "indexes"
    if idir.exists():
        for d in sorted(idir.iterdir()):
            if not d.is_dir():
                continue
            m = _read_json(d / "meta.json")
            if not m:
                continue
            ok = (d / "index.faiss").exists() and (d / "index.pkl").exists()
            indexes.append(
                {
                    "key": d.name,
                    "books": ", ".join(m.get("books", [])),
                    "n_total": m.get("n_total"),
                    "dim": m.get("dim"),
                    "built": m.get("built"),
                    "size_mb": round(
                        sum(f.stat().st_size for f in d.iterdir() if f.is_file())
                        / 1024 / 1024,
                        2,
                    ),
                    "complete": ok,
                }
            )

    if verbose:
        print(f"缓存根目录：{CACHE_ROOT}")
        print(f"\n== 已向量化的文本（books/）：{len(books)} 项 ==")
        if books:
            print(f"{'key':<18}{'文件':<18}{'块数':>6}{'维度':>6}  {'模型':<24}{'大小MB':>7}  状态")
            for b in books:
                print(
                    f"{b['key']:<18}{str(b['file']):<18}{str(b['chunks']):>6}"
                    f"{str(b['dim']):>6}  {str(b['model']):<24}{b['size_mb']:>7}"
                    f"  {'OK' if b['complete'] else '不完整'}"
                )
        else:
            print("  （空）说明还没有向量化过任何文本")

        print(f"\n== 已组装的索引（indexes/）：{len(indexes)} 项 ==")
        if indexes:
            print(f"{'key':<18}{'包含文件':<28}{'总块数':>7}{'大小MB':>8}  构建时间")
            for i in indexes:
                print(
                    f"{i['key']:<18}{i['books']:<28}{str(i['n_total']):>7}"
                    f"{i['size_mb']:>8}  {i['built']}"
                )
        else:
            print("  （空）")

    return {"books": books, "indexes": indexes}
