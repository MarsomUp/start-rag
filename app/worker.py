"""后台 Worker：串行消费 jobs 表里的任务。

设计要点
--------
1. **队列就是 jobs 表**，不引入 Redis / Celery。原因：
   - 任务必须持久化 —— API 重启不能丢任务
   - Worker 和 API 之间要靠共享存储传递进度（前端轮询 jobs 表）
   单机单 Worker 的场景，SQLite 一个人就够了。

2. **必须串行消费。** bge-small 向量化已经吃满 4 个核，同时跑两个任务只会让
   两个都变慢（总吞吐不变，但每个用户的等待时间翻倍）。
   "并发上传"和"并发向量化"是两件完全不同的事。

3. **进程重启后残留的 running 任务会重新排队。**
   因为 rag_cache 有断点续算，重新排队不会白跑 —— 从断点接着算。
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import select, update

from app.db import SessionLocal
from app.models import (
    BOOK_PROCESSING,
    BOOK_UPLOADED,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    Book,
    Job,
    utcnow,
)
from app.services.ingestion import ingest_book

log = logging.getLogger("app.worker")

# 没有任务时的兜底轮询间隔。
# 正常情况下 enqueue() 会立刻唤醒 Worker，用不到这个等待。
POLL_INTERVAL = 2.0


class TaskQueue:
    """单进程内的任务队列：一个后台线程，串行消费 jobs 表。"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wakeup = threading.Event()
        self._state_lock = threading.Lock()
        self._current_job_id: int | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._wakeup.clear()
        self._requeue_stale_jobs()
        self._thread = threading.Thread(
            target=self._loop, name="ingest-worker", daemon=True
        )
        self._thread.start()
        log.info("后台 Worker 已启动（串行消费，一次只跑一个任务）")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wakeup.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        log.info("后台 Worker 已停止")

    @property
    def current_job_id(self) -> int | None:
        with self._state_lock:
            return self._current_job_id

    @property
    def is_alive(self) -> bool:
        """Worker 线程是否在跑（/api/admin/queue 用它做健康指示）。"""
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # 入队
    # ------------------------------------------------------------------
    def enqueue(self, job_id: int) -> None:
        """任务记录已经写进 jobs 表了，这里只需唤醒 Worker 立刻来看一眼。"""
        log.info("任务 %s 入队", job_id)
        self._wakeup.set()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _requeue_stale_jobs(self) -> None:
        """把上一个进程留下的 running 任务重新排队。"""
        with SessionLocal() as db:
            n = db.execute(
                update(Job)
                .where(Job.status == JOB_RUNNING)
                .values(status=JOB_QUEUED, message="进程重启，重新排队（会从断点继续）")
            ).rowcount
            # 对应的书回到 uploaded，避免界面永远卡在"处理中"
            db.execute(
                update(Book)
                .where(Book.status == BOOK_PROCESSING)
                .values(status=BOOK_UPLOADED)
            )
            db.commit()
        if n:
            log.warning("发现 %d 个上次没跑完的任务，已重新排队", n)

    def _claim_next(self) -> int | None:
        """取出一个排队中的任务并标记为 running。"""
        with SessionLocal() as db:
            job = db.scalars(
                select(Job).where(Job.status == JOB_QUEUED).order_by(Job.id).limit(1)
            ).first()
            if job is None:
                return None
            job.status = JOB_RUNNING
            job.started_at = utcnow()
            job.message = "开始处理"
            db.commit()
            return job.id

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._claim_next()
            except Exception:
                log.exception("取任务失败，2 秒后重试")
                time.sleep(2)
                continue

            if job_id is None:
                # 没任务就等：要么被 enqueue() 唤醒，要么最多等 POLL_INTERVAL
                self._wakeup.wait(POLL_INTERVAL)
                self._wakeup.clear()
                continue

            with self._state_lock:
                self._current_job_id = job_id
            try:
                self._process(job_id)
            except Exception:
                log.exception("处理任务 %s 时发生未捕获异常", job_id)
            finally:
                with self._state_lock:
                    self._current_job_id = None

    def _process(self, job_id: int) -> None:
        db = SessionLocal()
        try:
            job = db.get(Job, job_id)
            if job is None or job.status != JOB_RUNNING:
                return

            book = db.get(Book, job.book_id) if job.book_id is not None else None
            if book is None:
                job.status = JOB_FAILED
                job.error = "对应的书籍记录不存在（可能已被删除）"
                job.message = "任务失败"
                job.finished_at = utcnow()
                db.commit()
                log.error("任务 %s 失败：书籍记录不存在", job_id)
                return

            t0 = time.time()
            log.info("开始处理任务 %s：%s", job_id, book.title)
            # 进度由 ingest_book 内部的 on_progress 直接写进 jobs 表，这里不用管
            ingest_book(db, book, job)
            log.info(
                "任务 %s 结束：status=%s 块数=%s 耗时=%.1f 秒",
                job_id, job.status, book.n_chunks, time.time() - t0,
            )
        finally:
            db.close()


# ----------------------------------------------------------------------
# 全局单例
# ----------------------------------------------------------------------
_queue: TaskQueue | None = None
_queue_lock = threading.Lock()


def get_task_queue() -> TaskQueue:
    global _queue
    if _queue is None:
        with _queue_lock:
            if _queue is None:
                _queue = TaskQueue()
    return _queue


def enqueue_ingest(job_id: int) -> None:
    """给路由层用的便捷函数。"""
    get_task_queue().enqueue(job_id)
