# AGENTS.md

> **给 AI 协作者的项目说明书。** 先读完这一页，能省你半小时到几小时。
>
> 设计与「为什么这么设计」在 [`ARCHITECTURE.md`](ARCHITECTURE.md) ——
> **这里不重复它**，只写：怎么跑、怎么验、什么不能做、哪里会踩坑。
> （DSH 对注入的指令内容有 65536 字节预算，重复内容是纯浪费，而且必然漂移。）

---

## 1. 这是什么

一个基于 RAG 的知识库问答系统，两个界面：

- **管理端** `/admin` —— 上传书籍、看向量化进度、删除
- **用户端** `/chat` —— 提问，答案带引用的原文片段

架构上的核心特点：

- **上传是异步的**：接口只负责存文件 + 落一条任务记录就返回（实测 0.03 秒），
  向量化由进程内的后台 Worker **串行**执行。
- **向量缓存按「文件内容哈希」存**，不是按路径。所以同一本书换名字、换目录再传，
  都是秒级复用，不会重新向量化。
- **两个界面都是服务端渲染**（Jinja2 + HTMX，htmx 已内置到 `app/web/static/`，不依赖 CDN），
  前端几乎没有 JS。

技术栈：Python 3.11 · FastAPI + Starlette · SQLAlchemy 2.0 + SQLite(WAL) ·
FAISS + `BAAI/bge-small-zh-v1.5`(512 维) · LangChain + ChatDeepSeek · Jinja2 + htmx

---

## 2. 怎么跑、怎么验

```powershell
# Windows —— 会自动先杀掉遗留的 uvicorn 父子进程再启动
.\start.bat

# 问答需要 Key（上传/向量化不需要）
$env:DEEPSEEK_API_KEY='sk-...'

# CLI 版，保留作回归测试
python main.py
```

**改完代码后必须跑的自检**（缺一不可，这是本项目的回归基线）：

1. `python -m compileall -q app main.py` —— 语法
2. 启动后 `GET /api/health` 返回 200
3. `python main.py`（需要 dummy key，否则会卡在 getpass 等你输入）
   **必须看到 `[hit] 索引缓存命中`** —— 说明向量缓存没被搞坏
4. 上传一本几百字的小书 → `GET /api/admin/books` 里状态变成 `ready`
5. `POST /api/chat {"question": "..."}` → 200 且 `sources` 非空

> CLI 回归测试会卡住的原因：`main.py` 在没设 `DEEPSEEK_API_KEY` 时会走 `getpass`，
> 而 Windows 的 `getpass` 直接从控制台读键、**绕过 stdin 重定向**，`< NUL` 挡不住。

---

## 3. 硬性规则（改代码前先读）

1. **任务队列必须串行消费。** `bge-small` 向量化会吃满 4 个核，同时跑两个任务
   只会让两个都变慢（总吞吐不变，单个等待时间翻倍）。
   "并发上传"和"并发向量化"是两件事。见 `app/worker.py`。
2. **不要在 `async def` 端点里调用阻塞的 embed / LLM。**
   `RagEngine.ask()` 是同步阻塞的，写成 `async def` 会卡死事件循环。
   用 `def` —— FastAPI 会把它丢进线程池。
3. **JSON API 和 HTMX 界面必须共用 service 层，不允许两份业务逻辑。**
   已有的模板：`services/ingestion.register_upload()`（上传）、
   `services/chat.ask_question()`（问答）。
   新增功能照这个模式：service 抛自定义异常，路由层负责翻译成
   `HTTPException`（JSON）或错误片段（HTML）。
4. **HTMX 片段的错误返回 HTTP 200 + 一张错误卡片**，不要 4xx/5xx。
   这样错误会出现在会话流里，前端不需要任何错误处理代码。
5. **模型必须是单例。** 每个请求 `new HuggingFaceEmbeddings(...)` = 反复加载 183MB 权重。
   见 `services/embeddings.py`。`RagEngine` 同理。
6. **LLM 输出用 `{{ }}` 转义，绝不加 `|safe`。** 换行靠 CSS `white-space: pre-wrap`。
7. **管理端只允许存在一条轮询循环。** 现在只有书籍列表一条（`_book_list.html`，自适应 1s/15s）。
   队列徽标是靠它**带外交换**（`hx-swap-oob`）更新的，没有自己的轮询。
   再加自动刷新的片段前，先想清楚会不会又多一条常驻轮询。
8. **缓存写入失败不能影响主流程。** `rag_cache` 的进度回调已经 try/except 包住了，
   别把这个保护去掉。

---

## 4. 已知陷阱（都是真实踩过的，别再踩第二遍）

### 4.1 `start.bat` 必须 CRLF + UTF-8，`chcp 65001` 在第一行中文之前

**这是本项目最容易改坏的文件。** 用普通编辑器或 `write`/`edit` 工具改它，
会把行尾写成 **LF**，`cmd.exe` 立刻报一堆：

```
'else' is not recognized as an internal or external command
'internal' is not recognized ...
```

那些报错信息**具有极强的误导性** —— 看起来像语法错误，实际是行尾符错了。

改完必须把行尾转回 CRLF：

```powershell
$p='start.bat'; $u=[Text.UTF8Encoding]::new($false)
$t=[IO.File]::ReadAllText($p,$u) -replace "`r`n","`n" -replace "`n","`r`n"
[IO.File]::WriteAllBytes($p,$u.GetBytes($t))
```

**`start.sh` 相反，必须是裸 LF**（CRLF 会让 Linux 上 `\r` 被当成命令的一部分）。

### 4.2 `HF_*` 环境变量必须在导入任何 HF 模块之前设置

`HF_ENDPOINT` / `HF_HOME` / `NO_PROXY` 统一在 `app/__init__.py` 里用
`os.environ.setdefault` 设置，**这是包入口，保证早于一切导入**。

原因：`huggingface_hub.constants` 是**模块加载时读一次**的（见其
`constants.py:69` 和 `:160`），晚了就不生效。同理 `app/config.py` 里的 `.env`
**不能**用来配这几个变量 —— 它加载得太晚。

### 4.3 本机网络环境很特殊

- 系统代理 `127.0.0.1:10090` **通常没启动**，而 `httpx` 会读系统代理 →
  请求被拒（`WinError 10061`）。靠 `NO_PROXY=*` 绕开。
- `huggingface.co` **直连不通**（TCP 能连上，但 TLS 证书吊销检查失败
  `0x80092013`）。走镜像 `HF_ENDPOINT=https://hf-mirror.com`，实测可用。
- `pypi.org` / `api.deepseek.com` 可直连。
- 模型权重缓存在 **`D:\AI\HF-Home`**（`HF_HOME`），不要在 C 盘。

### 4.4 `torch` 依赖 MSVC 运行库

本机已装 `vc_redist.x64` 14.51。如果换机器后 `import torch` 报
`WinError 1114`（c10.dll）或 `WinError 126`（torch_cpu.dll），
根因是缺 `msvcp140.dll ≥ 14.38` 或完全缺 `vcruntime140_threads.dll`
（torch 2.13+ 的 Windows wheel 需要 VC++ ≥ 14.38）。
**先查运行库版本，不要去改代码。**

### 4.5 `k` 太小，会让"能回答的问题"静默变成"查不到"

实测：《三国演义》里问「曹操的别名是什么」，正确答案那一块在相似度
**第 7~8 名** —— `k=5` 直接截掉，模型只能回"查不到"，
而**日志里一点异常都没有**，看起来完全像系统坏了。

排查手法：拿关键词去原文统计出现次数，确认事实在库里，再加大 `k` 看排名。
当前默认 `settings.top_k = 10`（`app/config.py`）。

### 4.6 展示用的名字别直接用存储用的文件名

上传的文件在磁盘上是 `<sha12>_原名.txt`（防同名覆盖），而 `rag_cache` 给块打的
`book` 标签就是文件 stem。所以要在 `services/chat.py` 里映射回数据库的 `title` ——
**映射放在读取侧**，这样已经缓存好的旧数据也一起修好了。

### 4.7 `--reload` 只监视 `*.py`

uvicorn 的 `StatReload` 实现是 `reload_dir.rglob("*.py")`。
所以 Worker 写 `.npy`/`.json` 缓存**不会**触发重启，不用担心
"写缓存 → 重启 → 任务中断 → 再写缓存"的死循环。

### 4.8 残留任务必须在 Worker 启动时重新排队

进程被强杀时任务会永远停在 `running`，界面上永远转圈。
`worker._requeue_stale_jobs()` 负责把它们改回 `queued`；
因为 `rag_cache` 有断点续算，重排不会白跑 —— 实测从 192/600 继续。
**别删这个调用。**

### 4.9 Git 远端必须用 HTTPS，不要改成 SSH

远端：`https://github.com/MarsomUp/start-rag.git`（默认分支 `main`）

本机对 GitHub 的干扰方式很特别。实测结论（2026-10，网络环境可能变化）：

| 入口 | 结果 |
|---|---|
| **HTTPS push** | ✅ 可用（GCM 凭据已存，不需要再登录） |
| SSH `git@github.com:22` | ❌ 认证成功，但 push 必失败 |
| SSH `ssh.github.com:443` | ❌ 同上 |

SSH 的失败现象**极具迷惑性**，别误判成自己配置错了：

```
Transferred: sent 3620, received 3932 bytes, in 0.8 seconds
debug1: Exit status 0                                    ← ssh 自己退出码是 0
send-pack: unexpected disconnect while reading sideband packet
fatal: the remote end hung up unexpectedly
```

拆开看就是：TCP 通 → SSH 握手通 → 认证通（会打印 `Hi MarsomUp`）→
**`git ls-remote`（读）通** → 但 **`git-receive-pack`（写）刚发出 3.6KB 就被远端关闭**。
换 `IPQoS=0`、`protocol.version=1`、端口 22/443 四种组合**全部无效**；
把提交拆到只剩 192KB 也照样失败 —— 所以**不是体积问题，是写通道被干扰**。

> 如果哪天 push 报 `Connection was reset`、`Empty reply from server` 或超时，
> 那是**间歇性**的（同一分钟内 `https://github.com` 可能又通了）。
> **重试即可**，不要急着换协议 —— 换 SSH 只会更糟。

另外本机 `https://github.com` 的**网页访问**经常超时，但 `api.github.com` 一直稳定。
所以想核实远端状态时，**用 API 比用网页可靠**：

```powershell
curl.exe -s https://api.github.com/repos/MarsomUp/start-rag/git/trees/main?recursive=1
```

---

## 5. 目录导航

```
app/
  __init__.py        ★ HF_*/NO_PROXY 环境变量必须在这里设（早于一切导入）
  main.py            FastAPI 入口：挂路由 + lifespan 里启停 Worker + 日志配置
  config.py          全部可调参数（pydantic-settings）
  db.py              引擎 + WAL + busy_timeout（API 和 Worker 会同时写 jobs 表）
  models.py          Book / Job / User / ChatLog + 状态机常量
  templating.py      共享 Jinja2 对象（独立模块，避免循环导入）
  rag_cache.py       ★ 核心资产：两级向量缓存 + 断点续算 + on_progress 回调
  worker.py          后台 Worker：串行消费 jobs 表
  routers/
    books.py         JSON API：上传/列表/删除/进度/队列
    chat.py          JSON API：问答 + 可检索书籍
    admin_ui.py      HTMX：管理端页面 + 片段
    chat_ui.py       HTMX：问答页 + 片段
  services/
    ingestion.py     上传登记 + 向量化（API 与界面共用）
    chat.py          问答：选书→检索→生成→记账（API 与界面共用）
    rag.py           RAG 引擎（prompt 模板也在这里）
    embeddings.py    向量模型单例
  web/templates/     4 个页面：base / index / admin / chat
                     6 个 HTMX 片段（下划线开头）：_book_list / _book_list_response
                     / _queue_badge / _upload_error / _exchange / _chat_error
  web/static/        htmx.min.js（本地内置）
data/                app.db + books/<sha12>_原名.txt   ← gitignore
.rag_cache/         向量缓存（books/ 单书向量 + indexes/ 组合索引）   ← gitignore
main.py              CLI 版入口（回归测试用，不要删）
start.bat/start.sh   启动脚本：先杀 uvicorn 父子进程再启动
examples/            学习用示例（如 bm25_demo.py）
```

---

## 6. 当前进度与下一步

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 代码拆成 `app/` 包，CLI 与 Web 共用 RAG 引擎 | ✅ |
| 1 | FastAPI 骨架 + SQLite + 上传接口 + 模板链路 | ✅ |
| 2 | 后台 Worker + 任务队列 + 进度写库 | ✅ |
| 3 | 管理端界面（上传/列表/进度条/删除） | ✅ |
| 4 | 用户问答界面 + `/api/chat`（含引用来源） | ✅ |
| 5 | 登录 + 角色隔离（`users` 表已建好） | ⬜ |
| 6 | 打磨：取消任务、失败重试、SSE、Docker | ⬜ |

**另一个待办（实测收益最大）：查询改写（multi-query）。**
诊断结论：很多"查不到"不是检索方式的问题，而是**查询里缺少原文用词**。
「曹操的别名是什么」用 BM25 实测排到 429/1472 名，而把查询改写成
「曹操 孟德 阿瞒」后 BM25 直接排第 1 名。
所以下一步应该是：提问时先用 LLM 把问题扩写成 2~3 个检索式，再融合检索结果。
（细节见 `ARCHITECTURE.md` 第 11 节第 11 条。）

进度变化时**同时更新这张表和 `ARCHITECTURE.md` 的路线表**。

---

## 7. 与用户协作的方式

- 用户是中文母语，**所有回复、注释、文档都用中文**。
- 用户偏好：**先给证据再给结论**。这个项目里凡是"实测过"的结论都保留在注释和
  文档里（含具体命令和数字），新增结论请沿用这个习惯。
- 用户会**自己改代码**（例如改过 `services/rag.py` 里的 prompt 措辞）。
  改动前先读文件当前内容，不要假设它还是你上次写的样子。
- 不确定的事**先验证再断言**。本项目已有多处"一开始猜错、实测推翻"的记录
  （例如"缺 bge 查询指令"的猜测是错的；"BM25 能治曹操别名问题"的结论也是错的）。
