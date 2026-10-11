"""集中配置。

所有可调参数都从这里出，不要散落在业务代码里。
优先级：环境变量 / .env  >  这里的默认值。
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from app import PROJECT_ROOT


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- 目录 ----------
    project_root: Path = PROJECT_ROOT
    data_dir: Path = PROJECT_ROOT / "data"
    books_dir: Path = PROJECT_ROOT / "data" / "books"
    db_path: Path = PROJECT_ROOT / "data" / "app.db"

    # ---------- 向量模型 ----------
    # bge-small：24M 参数 / 512 维，西游记全书约 3 分钟
    # bge-large：326M 参数 / 1024 维，效果更好但要 1.5~3 小时
    model_name: str = "BAAI/bge-small-zh-v1.5"
    device: str = "cpu"
    chunk_size: int = 600
    chunk_overlap: int = 80
    batch_size: int = 32
    top_k: int = 10
    """每次检索取几条。

    调成 10 是实测出来的：像「曹操的别名是什么」这种问题，正确答案的那一块
    排在相似度第 7~8 名 —— k=5 会把它截掉，于是模型只能回答"查不到"。
    k 调大代价只是 prompt 变长一点（每块最多 600 字）。
    """

    # ---------- LLM ----------
    deepseek_api_key: str = ""          # 读环境变量 DEEPSEEK_API_KEY
    deepseek_model: str = "deepseek-chat"
    llm_temperature: float = 0.0
    llm_max_retries: int = 2

    # ---------- 上传限制 ----------
    max_upload_mb: int = 50
    allowed_suffixes: tuple[str, ...] = (".txt", ".md")

    def ensure_dirs(self) -> None:
        """把运行期需要的目录建出来。"""
        for d in (self.data_dir, self.books_dir, self.db_path.parent):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
