"""管理端：书籍上传与管理。

上传是**异步**的：接口只负责"存文件 + 落一条任务记录 + 唤醒 Worker"就返回，
真正的向量化由 app/worker.py 在后台串行执行。
前端拿返回的 job_id 轮询 GET /api/admin/jobs/{id} 就能看到进度。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import JOB_QUEUED, JOB_RUNNING, Book, Job
from app.schemas import BookOut, JobOut, UploadResponse
from app.services import ingestion
from app.worker import get_task_queue

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ----------------------------------------------------------------------
# 上传
# ----------------------------------------------------------------------
@router.post("/books", response_model=UploadResponse, summary="上传一本书并入库")
def upload_book(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    # 业务逻辑全在 services/ingestion.register_upload 里
    # （HTMX 界面走的是同一个函数，不会有两份实现）
    try:
        book, job, dedup = ingestion.register_upload(
            db, file.filename or "book.txt", file.file.read()
        )
    except ingestion.UploadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return UploadResponse(
        book_id=book.id,
        job_id=job.id,
        status=book.status,          # uploaded = 已排队
        dedup=dedup,
        queued=not dedup,
        message=(
            "这本书之前已经向量化过，直接复用缓存"
            if dedup
            else "已加入队列，正在后台向量化；用 GET /api/admin/jobs/{job_id} 查进度"
        ),
    )


# ----------------------------------------------------------------------
# 查询
# ----------------------------------------------------------------------
@router.get("/books", response_model=list[BookOut], summary="书籍列表")
def list_books(db: Session = Depends(get_db)):
    return list(db.scalars(select(Book).order_by(Book.id.desc())))


@router.get("/books/{book_id}", response_model=BookOut, summary="单本书详情")
def get_book(book_id: int, db: Session = Depends(get_db)):
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="书籍不存在")
    return book


@router.delete("/books/{book_id}", summary="删除书籍")
def delete_book(book_id: int, db: Session = Depends(get_db)):
    """只删数据库记录和原始文件。

    .rag_cache 里的向量是按**内容哈希**存的，故意不删 ——
    同一本书重新上传会立刻命中缓存，不用再等几分钟。
    """
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="书籍不存在")

    Path(book.file_path).unlink(missing_ok=True)
    db.delete(book)
    db.commit()
    return {"deleted": book_id}


# ----------------------------------------------------------------------
# 任务进度（阶段 2 前端就轮询这个）
# ----------------------------------------------------------------------
@router.get("/jobs", response_model=list[JobOut], summary="任务列表")
def list_jobs(limit: int = 50, db: Session = Depends(get_db)):
    return list(db.scalars(select(Job).order_by(Job.id.desc()).limit(limit)))


@router.get("/jobs/{job_id}", response_model=JobOut, summary="任务进度")
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@router.get("/queue", summary="队列状态（管理员界面顶部的概览）")
def queue_status(db: Session = Depends(get_db)):
    """返回当前是否有任务在执行、还有多少在排队。"""
    queued = db.scalar(
        select(func.count()).select_from(Job).where(Job.status == JOB_QUEUED)
    )
    running = db.scalars(
        select(Job).where(Job.status == JOB_RUNNING).order_by(Job.id).limit(1)
    ).first()
    return {
        "queued": int(queued or 0),
        "worker_alive": get_task_queue().is_alive,
        "running": (
            {
                "job_id": running.id,
                "book_id": running.book_id,
                "progress": running.progress,
                "total": running.total,
                "message": running.message,
            }
            if running is not None
            else None
        ),
    }
