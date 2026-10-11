"""入库服务：把上传的文件变成可检索的向量库。

这是整个系统里唯一的"重活"，所以它必须能被后台 Worker 复用。

阶段 1：在请求线程里同步跑完。FastAPI 的 `def` 端点会跑在线程池里，不会阻塞事件循环。
阶段 2：把 ingest_book() 原样搬进 Worker。函数已经预留 on_progress，
        Worker 用它把 (已完成, 总数) 写进 jobs 表，前端再轮询展示。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    BOOK_FAILED,
    BOOK_PROCESSING,
    BOOK_READY,
    JOB_DONE,
    JOB_FAILED,
    JOB_RUNNING,
    Book,
    Job,
    utcnow,
)
from app.rag_cache import ProgressCb, build_or_load
from app.services.embeddings import get_embeddings

_UNSAFE_CHARS = re.compile(r"[^\w.\-]+")


def sha256_file(path: Path, block: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(block), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_filename(name: str) -> str:
    """只保留文件名部分并清掉危险字符，防 ../ 路径穿越和非法文件名。"""
    base = Path(name).name
    cleaned = _UNSAFE_CHARS.sub("_", base).strip("._")
    return (cleaned or "book.txt")[:120]


def save_upload(filename: str, content: bytes) -> tuple[Path, str]:
    """落盘，返回 (最终路径, sha256)。

    用内容哈希做前缀，避免同名文件互相覆盖（西游记.txt 传两次不会打架）。
    """
    settings.ensure_dirs()
    final_name = safe_filename(filename)

    tmp = settings.books_dir / f".uploading_{final_name}"
    tmp.write_bytes(content)
    digest = sha256_file(tmp)

    target = settings.books_dir / f"{digest[:12]}_{final_name}"
    tmp.replace(target)
    return target, digest


class UploadError(Exception):
    """上传登记失败（后缀不对、太大、空文件……）。

    服务层不关心 HTTP，所以抛这个自定义异常，由路由层翻译成
    HTTPException（JSON 接口）或错误片段（HTMX 界面）。
    """

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def register_upload(db: Session, filename: str, content: bytes) -> tuple[Book, Job, bool]:
    """上传登记的公共逻辑：校验 → 落盘 → 去重 → 建记录 → 入队。

    ★ JSON 接口（/api/admin/books）和 HTMX 界面（/admin/partials/books）都调它，
      保证业务逻辑只有一份实现。

    Returns:
        (book, job, dedup)。dedup=True 表示命中了已有向量缓存，不会真的跑任务。

    Raises:
        UploadError: 校验不通过
    """
    settings.ensure_dirs()

    suffix = Path(filename).suffix.lower()
    if suffix not in settings.allowed_suffixes:
        raise UploadError(
            f"只支持 {'/'.join(settings.allowed_suffixes)} 文件，收到 {suffix or '无后缀'}"
        )

    limit = settings.max_upload_mb * 1024 * 1024
    if len(content) > limit:
        raise UploadError(f"文件超过 {settings.max_upload_mb} MB 上限", status_code=413)
    if not content.strip():
        raise UploadError("文件内容为空")

    path, digest = save_upload(filename, content)

    # ---- 去重：内容哈希一致且已就绪 → 直接复用缓存，不重复向量化 ----
    existing = db.scalar(select(Book).where(Book.file_sha256 == digest))
    if existing is not None and existing.status == BOOK_READY:
        job = Job(
            type="ingest",
            book_id=existing.id,
            status=JOB_DONE,
            progress=existing.n_chunks or 0,
            total=existing.n_chunks or 0,
            message="复用已有向量缓存",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return existing, job, True

    book = Book(
        title=Path(filename).stem,
        filename=filename,
        file_path=str(path),
        file_sha256=digest,
        file_size=len(content),
    )
    db.add(book)
    db.commit()
    db.refresh(book)

    job = Job(type="ingest", book_id=book.id, message="排队中")
    db.add(job)
    db.commit()
    db.refresh(job)

    # 局部导入：app.worker 依赖本模块（它要用 ingest_book），模块级导入会成环
    from app.worker import enqueue_ingest

    enqueue_ingest(job.id)

    return book, job, False


def ingest_book(
    db: Session,
    book: Book,
    job: Job | None = None,
    on_progress: ProgressCb | None = None,
) -> Book:
    """向量化一本书并落库。可被同步调用，也可以被后台 Worker 调用。

    Args:
        db: 数据库会话（由调用方管理生命周期）
        book: 待处理的书籍记录
        job: 对应的任务记录，用于上报进度；可以为 None
        on_progress: 额外的进度回调（阶段 2 的 Worker 用）
    """
    book.status = BOOK_PROCESSING
    book.error = None
    if job is not None:
        job.status = JOB_RUNNING
        job.started_at = utcnow()
        job.message = "正在切分与向量化"
    db.commit()

    def report(done: int, total: int) -> None:
        """每批向量化完成后被 rag_cache 回调一次。"""
        if job is not None:
            job.progress = done
            job.total = total
            job.message = f"正在向量化 {done}/{total}"
            db.commit()
        if on_progress is not None:
            on_progress(done, total)

    try:
        # 复用 rag_cache：命中缓存时它会直接返回，一行都不算
        _, info = build_or_load(
            [book.file_path],
            embeddings=get_embeddings(),
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            batch_size=settings.batch_size,
            on_progress=report,
            verbose=False,
        )

        # info 的两种路径（命中/未命中）结构一致，但这里仍然用 .get 兜一下，
        # 避免将来 rag_cache 再改字段时把整条入库链路打挂
        per_book = (info.get("books") or [{}])[0]
        book.n_chunks = int(info.get("n_chunks") or 0)
        book.dim = int(info.get("dim") or 0)
        book.cache_key = per_book.get("key")
        book.model_name = settings.model_name
        book.chunk_size = settings.chunk_size
        book.chunk_overlap = settings.chunk_overlap
        book.status = BOOK_READY
        book.error = None

        if job is not None:
            job.status = JOB_DONE
            job.progress = job.total = book.n_chunks
            job.message = f"完成，共 {book.n_chunks} 个文本块"
            job.finished_at = utcnow()

    except Exception as exc:  # noqa: BLE001  失败原因要原样留给管理员看
        book.status = BOOK_FAILED
        book.error = f"{type(exc).__name__}: {exc}"
        if job is not None:
            job.status = JOB_FAILED
            job.error = book.error
            job.message = "向量化失败"
            job.finished_at = utcnow()

    db.commit()
    db.refresh(book)
    return book
