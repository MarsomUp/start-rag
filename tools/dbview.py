"""SQLite 数据库查看工具（不需要装任何东西，Python 自带 sqlite3 模块）。

用法：
    python tools/dbview.py                      总览：所有表 + 行数 + 字段
    python tools/dbview.py schema [表名]         建表语句（默认全部）
    python tools/dbview.py show <表名> [行数]    看某张表的数据（默认 20 行）
    python tools/dbview.py sql "SELECT ..."     执行任意 SQL
    python tools/dbview.py files                数据库相关文件（含 WAL）

默认**只读打开**，防止你手滑改坏应用的数据。要写就加 --write：
    python tools/dbview.py --write sql "DELETE FROM chat_logs"
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "app.db"

# 给每张表写一句人话说明 —— 第一次看库时最需要的就是这个
TABLE_NOTES = {
    "books": "上传的书籍（每行一本）。file_path 指向磁盘上的原始文件",
    "jobs": "向量化任务。Worker 从这张表里认领待办，进度写回这里",
    "users": "用户（阶段 5 做登录时才会用上，现在应该是空的）",
    "chat_logs": "每次提问的问答记录，含引用的原文片段",
}


# ----------------------------------------------------------------------
# 中文对齐：汉字占 2 个显示宽度，直接 len() 会让表格歪掉
# ----------------------------------------------------------------------
def _w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, n: int, right: bool = False) -> str:
    gap = " " * max(0, n - _w(s))
    return gap + s if right else s + gap


def _print_table(headers: list[str], rows: list[list[str]], max_width: int = 60):
    """打印一张对齐的表格。过宽的单元格会被截断（否则一行几百字没法看）。"""
    def cell(v):
        v = "" if v is None else str(v).replace("\n", " ").replace("\r", "")
        return v if _w(v) <= max_width else v[: max_width - 1] + "…"

    rows = [[cell(v) for v in r] for r in rows]
    widths = [_w(h) for h in headers]
    for r in rows:
        for i, v in enumerate(r[: len(widths)]):
            widths[i] = max(widths[i], _w(v))

    line = "  ".join(_pad(h, widths[i]) for i, h in enumerate(headers))
    print("  " + line)
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("  " + "  ".join(_pad(v, widths[i]) for i, v in enumerate(r[: len(widths)])))
    if not rows:
        print("  （没有数据）")


def connect(write: bool) -> sqlite3.Connection:
    if not DB.exists():
        sys.exit(f"数据库不存在：{DB}\n（先启动一次服务，或上传一本书）")
    if write:
        return sqlite3.connect(DB)
    # 只读 URI：既能安全查看，又不会干扰正在运行的服务（WAL 允许并发读）
    return sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)


def cmd_overview(con: sqlite3.Connection):
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    print(f"数据库：{DB}")
    print(f"大小  ：app.db {DB.stat().st_size:,} 字节", end="")
    wal = Path(str(DB) + "-wal")
    if wal.exists():
        print(f"  +  app.db-wal {wal.stat().st_size:,} 字节  ← 新数据在这里")
    else:
        print()
    print(f"共 {len(tables)} 张表")
    print()

    for t in tables:
        n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
        print(f"── {t}  （{n} 行）")
        print(f"   {TABLE_NOTES.get(t, '')}")
        print(f"   字段：{', '.join(cols)}")
        print()


def cmd_schema(con: sqlite3.Connection, table: str | None):
    sql = ("SELECT name, sql FROM sqlite_master WHERE type IN ('table','index') "
           "AND sql IS NOT NULL")
    params = ()
    if table:
        sql += " AND name = ?"
        params = (table,)
    rows = list(con.execute(sql + " ORDER BY name", params))
    if not rows:
        print("没找到。用 `python tools/dbview.py` 看有哪些表。")
        return
    for name, ddl in rows:
        print(f"── {name}")
        print(ddl)
        print()


def cmd_show(con: sqlite3.Connection, table: str, limit: int):
    try:
        cur = con.execute(f'SELECT * FROM "{table}" LIMIT {limit}')
    except sqlite3.OperationalError as e:
        sys.exit(f"查询失败：{e}\n用 `python tools/dbview.py` 看有哪些表。")
    headers = [d[0] for d in cur.description]
    rows = [list(r) for r in cur.fetchall()]
    total = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    print(f"── {table}  共 {total} 行，显示前 {len(rows)} 行")
    print()
    _print_table(headers, rows)


def cmd_sql(con: sqlite3.Connection, statement: str):
    cur = con.execute(statement)
    if cur.description:
        headers = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
        _print_table(headers, rows)
        print(f"\n  ({len(rows)} 行)")
    else:
        con.commit()
        print(f"  影响 {cur.rowcount} 行（已提交）")


def cmd_files():
    print("── data/ 目录")
    for p in sorted(DB.parent.glob("app.db*")):
        what = {"app.db": "主数据库文件",
                "app.db-wal": "预写日志：还没合并回主文件的改动",
                "app.db-shm": "共享内存索引：WAL 的辅助文件"}.get(p.name, "")
        print("  {:<16} {:>10,} 字节   {}".format(p.name, p.stat().st_size, what))
    print()
    print("  ⚠️  拷贝备份时必须三个文件一起，或者先停掉服务再拷 ——")
    print("      只拷 app.db 会丢掉 WAL 里还没合并的数据。")


def main():
    ap = argparse.ArgumentParser(
        description="查看 data/app.db 的内容（默认只读）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--write", action="store_true", help="允许写操作（默认只读）")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("schema", help="看建表语句")
    p.add_argument("table", nargs="?")

    p = sub.add_parser("show", help="看某张表的数据")
    p.add_argument("table")
    p.add_argument("limit", nargs="?", type=int, default=20)

    p = sub.add_parser("sql", help="执行任意 SQL")
    p.add_argument("statement")

    sub.add_parser("files", help="看数据库相关文件")

    args = ap.parse_args()
    con = connect(args.write)
    try:
        if args.cmd == "schema":
            cmd_schema(con, args.table)
        elif args.cmd == "show":
            cmd_show(con, args.table, args.limit)
        elif args.cmd == "sql":
            cmd_sql(con, args.statement)
        elif args.cmd == "files":
            cmd_files()
        else:
            cmd_overview(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
