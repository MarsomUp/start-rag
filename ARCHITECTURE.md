# 架构设计 · 知识库问答系统

> 一份可以边写边对照的活文档。每完成一个阶段就回来更新"实现进度"。

## 1. 需求

1. 能**单独异步加载**书籍（一本处理中不影响其他书、不阻塞界面）
2. 问答界面做成 **B 端产品**
3. 两个界面：**用户端**（问答）、**管理端**（上传与管理）

---

## 2. 总体架构

```
┌──────────────────────────────────────────────────────────┐
│  前端：两个界面（Jinja2 + HTMX，htmx 已内置到本地 static）  │
│   /chat     用户端：问答 + 引用来源 + 历史                  │
│   /admin    管理端：上传 / 书籍列表 / 进度 / 删除            │
└───────────────────────┬──────────────────────────────────┘
                        │ REST + 轮询进度
┌───────────────────────▼──────────────────────────────────┐
│  API 层  FastAPI（app/routers/）                          │
│   books.py   上传 / 列表 / 删除 / 任务进度                 │
│   chat.py    问答（阶段 4）                                │
│   auth.py    登录与角色（阶段 5）                           │
└───────────────────────┬──────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│  服务层（app/services/，不碰 HTTP、不碰 session 来源）       │
│   ingestion.py  切分→向量化→建索引（★ 阶段 2 搬进 Worker）  │
│   rag.py        检索 + 生成，CLI 与 Web 共用                │
│   embeddings.py 向量模型单例（避免每请求重新加载权重）        │
└────────┬──────────────────────────────┬─────────────────┘
         │ 入队（阶段 2）                 │
┌────────▼─────────┐        ┌───────────▼─────────────────┐
│  后台 Worker      │        │  存储层                      │
│  串行消费任务队列  │        │  SQLite(WAL)：books/jobs/... │
│  （线程 → 独立进程）│        │  磁盘：data/books + .rag_cache│
└──────────────────┘        └─────────────────────────────┘
```

**核心原则：API 永远不干重活。** 上传接口只负责"存文件 + 落一条任务记录"就返回，
向量化交给后台 Worker。

---

## 3. 为什么"异步加载"是支点

一本书的加载 = 上传 → 解析 → 切分 → **向量化（分钟级）** → 建索引。

| 如果不异步 | 后果 |
|---|---|
| 在请求里直接向量化 | HTTP 超时、管理端界面卡死 |
| 不落任务表，只在内存里跑 | API 一重启任务就丢、进度看不见 |
| 多个任务并发向量化 | 每个都变慢，总吞吐不变（见坑 1） |

所以逼出三样缺一不可的东西：**任务表（持久化）+ Worker（串行）+ 进度上报（可轮询）**。

---

## 4. 已有资产（不用重写）

| 模块 | 作用 |
|---|---|
| `app/rag_cache.py` | **两级缓存**：`books/<book_key>/` 存单书向量、`indexes/<组合key>/` 存组合索引；分批向量化 + 断点续算 + `on_progress` 回调 |
| `app/services/rag.py` | 检索 + 生成，CLI 和 Web 共用 |
| `app/services/embeddings.py` | 向量模型单例 |

**关键性质：缓存键是「文件内容哈希」，不是路径。** 所以：
- 同一本书换名字/换目录上传 → 秒级复用
- 每本书一旦自己的 job 完成就**立刻可查**，不需要等全局重建

---

## 5. 目录结构

```
start-rag/
├─ app/
│  ├─ __init__.py          ★ HF_*/NO_PROXY 环境变量必须在这里设（导入前生效）
│  ├─ main.py              FastAPI 入口，挂路由 + 启停 Worker
│  ├─ config.py            pydantic-settings 集中配置
│  ├─ db.py                引擎 + WAL + session
│  ├─ models.py            Book / Job / User / ChatLog
│  ├─ schemas.py           API 请求响应模型
│  ├─ templating.py        共享的 Jinja2 模板对象（避免循环导入）
│  ├─ rag_cache.py         两级向量缓存（核心资产）
│  ├─ worker.py            后台 Worker：串行消费 jobs 表
│  ├─ routers/
│  │  ├─ books.py          JSON API：上传/列表/删除/进度/队列
│  │  ├─ chat.py           JSON API：问答 + 可检索书籍
│  │  ├─ admin_ui.py       HTMX 界面：管理端页面 + 片段
│  │  └─ chat_ui.py        HTMX 界面：问答页 + 片段
│  ├─ services/
│  │  ├─ ingestion.py      上传登记 + 向量化（被 API 和界面共用）
│  │  ├─ chat.py           问答（选书→检索→生成→记账，被 API 和界面共用）
│  │  ├─ rag.py            RAG 引擎
│  │  └─ embeddings.py     模型单例
│  └─ web/
│     ├─ templates/
│     │  ├─ base.html            布局 + 全部 CSS
│     │  ├─ index.html           落地页
│     │  ├─ admin.html           管理端页面
│     │  ├─ chat.html            问答页面
│     │  ├─ _book_list.html      ← HTMX 片段（自带轮询节奏）
│     │  ├─ _queue_badge.html    ← HTMX 片段
│     │  ├─ _upload_error.html   ← HTMX 片段
│     │  ├─ _exchange.html       ← HTMX 片段（一问一答）
│     │  └─ _chat_error.html     ← HTMX 片段
│     └─ static/htmx.min.js
├─ data/
│  ├─ app.db               SQLite
│  └─ books/               上传的原始书籍
├─ .rag_cache/             向量缓存（在项目根，和 data 平级）
├─ main.py                 CLI 版入口（回归测试用）
├─ start.bat / start.sh    启动脚本（先杀旧进程再启动）
└─ requirements.txt
```

---

## 6. 数据模型

```
books(id, title, filename, file_path, file_sha256 UNIQUE, file_size,
      status, n_chunks, dim, model_name, chunk_size, chunk_overlap,
      cache_key, error, created_at, updated_at)

jobs(id, type, book_id→books, status, progress, total, message, error,
     created_at, started_at, finished_at)

users(id, username UNIQUE, password_hash, role)      -- admin | user
chat_logs(id, user_id→users, question, answer, sources_json, elapsed_ms, created_at)
```

状态机：

```
book: uploaded → processing → ready
                          └→ failed
job : queued → running → done
                      └→ failed / canceled
```

**`books.file_sha256` 与 `rag_cache` 的 `book_key` 天然对齐** —— 重复上传直接复用，不重复向量化。

---

## 7. API 清单

```
GET    /api/health                    健康检查（公开）

# 登录（页面路由，不是 API）
GET    /login                         登录页
POST   /login                         表单登录 → 303 跳 next
POST   /logout                        登出

# 管理端（全部要求 role=admin）
POST   /api/admin/books               multipart 上传 → {book_id, job_id, status, queued, dedup}
                                      ★ 立刻返回，向量化在后台跑
GET    /api/admin/books               书籍列表
GET    /api/admin/books/{id}          单本详情
DELETE /api/admin/books/{id}          删除（向量缓存故意保留）
GET    /api/admin/jobs               任务列表
GET    /api/admin/jobs/{id}           任务进度 ← 前端轮询这个画进度条
GET    /api/admin/queue               队列概览 {queued, worker_alive, running}
                                      （界面不走它，见 10.5 的带外交换）
GET    /api/admin/users               用户列表（永不返回 password_hash）
POST   /api/admin/users               新建 {username, password, role}
POST   /api/admin/users/{id}/password 重置密码
DELETE /api/admin/users/{id}          删除（不能删自己 / 最后一个管理员）

# 用户端（要求登录，admin 或 user 都行）
POST   /api/chat                      {question, book_ids?, k?} → {answer, sources, elapsed}
GET    /api/books                     可检索的书籍（只有 status=ready 的）
```

---

## 8. 异步加载时序（已实现）

```
管理员              API                 Worker              rag_cache
  │ POST /books      │                    │                    │
  │─────────────────>│ 存文件 + 算 sha256  │                    │
  │                  │ sha 已存在且 ready? │                    │
  │                  │  → 直接返回旧 id    │                    │
  │                  │ INSERT book+job    │                    │
  │                  │ enqueue(job.id)    │                    │
  │<─ {id, job_id} ──│ 唤醒 Worker         │                    │
  │  (0.03 秒返回)    │                    │ 取一条 queued       │
  │                  │                    │ UPDATE→running     │
  │                  │                    │───────────────────>│
  │                  │                    │  切分 + 分批向量化   │
  │                  │                    │<─ on_progress(d,t) ─│
  │                  │                    │ UPDATE jobs.progress│
  │ GET /jobs/{id}   │                    │                    │
  │─────────────────>│ 读 jobs 表          │                    │
  │<─ 96/600 ────────│                    │                    │
  │ GET /jobs/{id}   │                    │                    │
  │<─ done ──────────│                    │                    │
  │  这本书立刻可问答 ✅│                    │                    │
```

**崩溃 / 重启恢复**（已实现并实测）：

进程被杀时任务会永远停在 `running`。Worker 启动时会把它们重新排队，
而且因为 `rag_cache` 有断点续算，**重新排队不会白跑**：

```
11:54:30 WARNING app.worker: 发现 1 个上次没跑完的任务，已重新排队
11:54:30 INFO    app.worker: 开始处理任务 1：重启测试
重启后第一次看到 running：progress = 192/600     ← 从断点继续，不是从 0
```

实测数据（600 块的书，在第 192 块处强杀进程）：

| 指标 | 结果 |
|---|---|
| 上传接口返回耗时 | **0.03 秒**（阶段 1 同步时要 13 秒） |
| 任务是否串行 | A 结束 `...46.227` → B 开始 `...46.236`，**间隔 9 毫秒** |
| 崩溃后是否自动重排 | 是（启动日志有 WARNING） |
| 是否从断点续算 | 是，`192/600` 起步，408 块只用 44 秒 |

---

## 9. 多书检索

```
查询:
  book_ids = 用户选中的书（默认 = 全部 ready）
  vs, info = build_or_load(选中的文件列表)    # 组合索引有缓存，命中即毫秒级
  docs = vs.similarity_search(q, k)
  → 拼 context → 调 LLM → 返回答案 + 引用
```

不需要手写 fan-out 合并 —— `rag_cache` 的"组合索引"缓存天然处理了这件事。
书多了（>10 本）再考虑全局索引或 Milvus/Qdrant/pgvector，**接口不用变**。

---

## 10. 实施路线

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 代码拆成 `app/` 包，CLI 与 Web 共用 RAG 引擎 | ✅ 已完成 |
| 1 | FastAPI 骨架 + SQLite + 上传接口（同步）+ 模板链路 | ✅ 已完成 |
| 2 | ★ 后台 Worker + 任务队列 + 进度写库 | ✅ 已完成 |
| 3 | 管理端界面：上传 / 列表 / 进度条 / 删除 | ✅ 已完成 |
| 4 | 用户问答界面 + `/api/chat`（含引用来源） | ✅ 已完成 |
| 5 | 登录 + 角色隔离（admin 才能传书） | ✅ 已完成 |
| 6 | 打磨：取消任务、失败重试、SSE、Docker | ⬜ 待做 |

**阶段 2（分水岭）已完成**，"单独异步加载"已经真正成立。

实现落在 `app/worker.py`，三个关键决定：

1. **队列就是 jobs 表**，不引入 Redis / Celery。
   任务本来就必须持久化（API 重启不能丢），进度本来就要靠共享存储传递，
   单机单 Worker 的场景 SQLite 一个人就够了。
2. **Worker 是 API 进程内的后台线程**（不是独立进程）。
   好处是一条命令就能起完整服务（start.bat 不用管两个进程），
   坏处是 API 重启会打断任务 —— 所以必须配套第 3 点。
3. **启动时把残留的 running 任务重新排队**，配合 rag_cache 的断点续算，
   重启的代价只有"重跑没算完的那一批"。

阶段 3 接下来要做的：管理端界面。接口全部就绪，前端拿 `job_id` 轮询
`GET /api/admin/jobs/{id}` 就能画进度条；`GET /api/admin/queue` 可以画队列概览。

---

## 10.5 管理端界面（阶段 3，已实现）

技术选型：**Jinja2 + HTMX + 服务端返回 HTML 片段**，前端几乎没有 JS。

### 为什么这么做

- 管理端就是典型 CRUD，不需要客户端状态。服务端渲染天然不会出现
  "删了一行但计数没变"这类不一致。
- htmx 已内置到 `app/web/static/htmx.min.js`（48KB），**不依赖任何 CDN**。

### 路由划分

```
/api/admin/*          JSON API（进 OpenAPI 文档，给程序和测试用）
/admin                页面骨架
/admin/partials/*     HTMX 片段（HTML，不进文档）
```

**关键：两边共用同一份业务逻辑** —— `services/ingestion.register_upload()`。
JSON 路由把 `UploadError` 翻译成 `HTTPException`，HTMX 路由翻译成错误片段。
绝不写两遍上传校验。

### 四个值得学的 htmx 技巧

1. **片段自带轮询节奏。** `_book_list.html` 返回的 `<tbody>` 上带着
   `hx-get` + `hx-trigger="every {{ poll_seconds }}s"` + `hx-swap="outerHTML"`。
   被换进页面后它会继续自我刷新，而节奏**由服务端决定**：

   | 状态 | 轮询间隔 |
   |---|---|
   | 有任务在跑 | 1 秒 |
   | 全部空闲 | 15 秒 |

   不需要单独轮询某个 job，也不需要前端定时器。

2. **`HX-Retarget` 响应头重定向。** 上传表单固定把响应换到 `#book-list`，
   但校验失败时后端加两个头就能把这次响应改投到 `#upload-error`：

   ```
   HX-Retarget: #upload-error
   HX-Reswap:   innerHTML
   ```

   前端代码一个字都不用改。

3. **删除也返回整个列表。** `hx-delete` 的响应依然是新的 `<tbody>`，
   行自动消失、计数自动更新，不需要前端自己摘 DOM。

4. **带外交换（`hx-swap-oob`）合并轮询循环。** 队列徽标原本自己
   `hx-trigger="every 3s"`，结果列表空闲了、徽标还在每 3 秒敲一次日志。
   现在徽标**不自己轮询**，而是由列表片段的响应顺带更新：

   ```html
   <!-- _book_list_response.html -->
   {% include "_book_list.html" %}      {# 主体：tbody #}
   {% set oob = true %}
   {% include "_queue_badge.html" %}    {# 带外：<span id="queue-badge" hx-swap-oob="true"> #}
   ```

   一个请求更新两处，整个管理端只剩**一条**轮询循环。

   > **教训：每加一个会自动刷新的片段，就多一条常驻轮询。**
   > 能合并就合并，或者至少让它在空闲时降频 ——
   > 否则页面上开着几个标签页，日志会被这些请求刷满。

### 实测

| 项 | 结果 |
|---|---|
| 上传接口返回 | 0.04 秒（HTMX 路径同样是异步的） |
| 进度条 | `0.0% → 8.7% → … → 95.4% → 完成` |
| 完成后轮询 | 自动从 1 秒降到 15 秒 |
| 上传 `.pdf` | `HX-Retarget: #upload-error`，显示"只支持 .txt/.md 文件" |
| 删除 | 返回新 tbody，行消失，计数正确 |
| 页面里的轮询循环数 | **1**（徽标改成带外交换后） |

---

## 10.6 用户问答界面（阶段 4，已实现）

同一套 htmx 模式，路由划分也一样：

```
/api/chat                  JSON API（进文档）
/chat                      页面
/chat/partials/ask         HTMX 片段
```

业务逻辑在 `services/chat.ask_question()`，两个路由共用。

### 几个设计决定

1. **`def` 而不是 `async def`。** `RagEngine.ask()` 是同步阻塞的
   （一次向量化 + 一次 LLM 调用）。写成 `async def` 会直接卡死事件循环，
   所有请求一起超时。FastAPI 会把 `def` 端点丢进线程池。

2. **错误也返回 HTTP 200 + 一张错误卡片。**
   这样错误会出现在会话流里，和正常回答长得一样。用户不用去翻浏览器控制台，
   前端也不需要任何错误处理代码。

3. **`hx-swap="beforeend"` 追加而非替换。** 会话区不断增长，形成对话历史，
   前端零状态管理。

4. **不要用 `form.reset()` 清空输入框。** 检索范围的复选框是用
   `form="ask-form"` 属性关联进表单的（它们在 `<details>` 里，不在 `<form>` 内），
   `reset()` 会把它们**全部取消勾选**。只清空 `[name=question]` 那一个。

5. **回答用 `{{ }}` 转义，绝不加 `|safe`。** 内容是 LLM 输出，
   换行靠 CSS `white-space: pre-wrap` 保留，不靠插 HTML。

### 实测

没有真实 API Key，所以测试时把 `RagEngine.ask()` 打了桩，
专注验证路由和模板渲染：

| 项 | 结果 |
|---|---|
| 无书时 | 页面给引导、按钮禁用；API 返回 400，HTMX 返回错误卡片 |
| 有书时 | 渲染检索范围复选框、书名；无 Key 时显示警告 |
| HTMX 提问 | 200，返回一问一答卡片（问题回显 + 回答 + 引用 `<details>` + 耗时/书目） |
| JSON 提问 | 200，`{answer, sources[2], elapsed, cache_status}` |
| 空白问题 | HTMX → "问题不能为空" 卡片；JSON → 422（Pydantic 拦截） |
| `book_ids=[999]` | 400 |
| `GET /api/books` | 只返回 `status=ready` 的书 |
| `chat_logs` | 每次提问都落库（含 sources_json、elapsed_ms、**user_id**） |

---

## 10.7 登录与角色隔离（阶段 5，已实现）

### 会话：签名 cookie，不引入 Redis

用 `starlette.middleware.sessions.SessionMiddleware`（底层 itsdangerous）——
会话内容 base64 编码后用 HMAC 签名塞进 cookie，**服务端什么都不存**。

cookie 里只有 `{"uid": 1}`，**刻意不放 role**。放了的话，改了用户角色要等
对方重新登录才生效；现在每次请求都用 uid 现查数据库，降级立刻生效。

> 为什么不用 JWT：这是同域的服务端渲染应用，没有跨服务验证签名的需求。
> 签名 cookie 更简单，而且浏览器自动管理，不用写任何前端存储逻辑。
> JWT 的优势（无状态、跨域）在这里都用不上，只是徒增复杂度。

签名密钥优先读 `RAG_SESSION_SECRET`，否则自动生成并存到 `data/.session_secret`。
**必须跨重启稳定**，不然每次重启把所有用户登出。

### 密码：bcrypt，不用 sha256

sha256 快得正是缺点 —— 显卡每秒能试几十亿次，数据库被拖走就等于明文。
bcrypt 故意慢、自带随机盐，所以**同一个密码哈希两次结果不同**，
不能用 `==` 比，必须 `checkpw`。

⚠️ bcrypt 只处理前 **72 字节**，超了直接报错。中文一个字 3 字节，
所以约 24 个汉字就到上限 —— `hash_password` 会提前拦下并给清楚的提示。

### 未登录时的响应：按请求类型分三种

这是最容易做错的地方。**对所有请求统一 302 是不行的**：

| 请求类型 | 响应 | 为什么 |
|---|---|---|
| htmx 片段（`HX-Request: true`） | `HX-Redirect` 头 | 直接 302 会被 htmx 当成"响应内容" swap 进目标元素，页面上出现一块莫名其妙的登录表单 |
| 页面（非 `/api/*`） | 303 跳 `/login?next=...` | 浏览器整页导航 |
| API（`/api/*`） | 401 JSON | 脚本要能拿到结构化的错误 |

判据用**路径**而不是 `Accept` 头 —— 这一点是实测纠正过来的：
最初写的是 `"text/html" in Accept`，看起来合理（浏览器导航确实带），
但 TestClient / curl / 不少脚本和代理发的是 `Accept: */*`，
于是同一个未登录请求，浏览器拿到 303、脚本拿到 401，行为随客户端而变。

htmx 的 `next` 还要特别处理：片段请求的 `request.url` 是**片段地址**
（如 `/admin/partials/books`），拿它当 next 的话登录后会看到一个裸片段。
所以优先用它自己发的 `HX-Current-URL`（当前**页面**地址）。

### 守卫放在 router 上，不放在函数上

```python
router = APIRouter(prefix="/api/admin", tags=["admin"],
                   dependencies=[Depends(require_admin)])
```

这样以后新增接口**不可能忘记加权限** —— 漏写一个函数参数是常见疏漏，
漏写路由级依赖则一眼能看出来。

### 首次启动引导管理员

`users` 表为空时自动建一个管理员：

- 用户名取 `RAG_ADMIN_USERNAME`（默认 `admin`）
- 密码取 `RAG_ADMIN_PASSWORD`；**留空则随机生成，并在启动日志里打印一次**

比写死 `admin/admin123` 安全得多 —— 那种默认密码是扫描器的第一目标。
忘了密码可以用 `python tools/adduser.py admin --reset <新密码>` 改。

### 访问矩阵（实测）

| | 未登录 | 普通用户 | 管理员 |
|---|---|---|---|
| `/` `/login` `/api/health` | 200 | 200 | 200 |
| `/chat` `/api/chat` `/api/books` | 303 / 401 | 200 | 200 |
| `/admin` `/api/admin/*` | 303 / 401 | **403** | 200 |
| `/users` `/api/admin/users` | 303 / 401 | **403** | 200 |

`/docs` 目前是公开的（只是接口文档，不含数据），阶段 6 可以一起收紧。

### 两条不可逆操作的保护

1. **不能删自己** —— 管理员手一滑就把自己锁在门外
2. **不能删/降级最后一个管理员** —— 顺带被第 1 条覆盖（自己必然是管理员）

删除用户和降级角色都在 service 层拦，不靠界面提示。

---

## 11. 踩坑清单

1. **别让多个任务并发向量化。** bge-small 已经吃满 4 个核，同时跑 2 个只会让两个都变慢。
   队列必须**串行消费** —— "并发上传"和"并发向量化"是两件事。
2. **别在 `async def` 里直接调 `embed_documents`。** 它是同步阻塞的，会卡死事件循环。
   要么用 `def` 端点（FastAPI 会丢进线程池），要么丢进 Worker。
3. **模型要单例。** 每个请求 new 一个 `HuggingFaceEmbeddings` = 反复加载 183MB 权重。
4. **SQLite 必须开 WAL + busy_timeout。** API 和 Worker 会同时写 `jobs` 表。
5. **上传要限制大小和后缀**，存盘文件名要清危险字符（防 `../` 穿越）。
6. **HF_ENDPOINT / HF_HOME 必须在导入 HF 模块之前设置**（`app/__init__.py`），
   因为 `huggingface_hub.constants` 是模块加载时读一次的。
7. **进程内 Worker 必须处理"残留任务"。** 线程随 API 进程一起死，任务会永远卡在
   `running`。启动时要把它们重新排队，否则界面上永远转圈。
8. **进度要在切分完就报一次 `(0, 总数)`。** 否则"加载模型 + 切分 + 跑完第一批"
   这段时间界面只能显示 `0/0`，实测要等 36 秒，看起来完全像卡死。
9. **`--reload` 只监视 `*.py`。** 已确认 uvicorn 的 StatReload 是
   `reload_dir.rglob("*.py")`，所以 Worker 写 `.npy` / `.json` 缓存不会触发重启，
   不用担心"写缓存 → 重启 → 任务中断 → 再写缓存"的死循环。
10. **后台线程的日志要自己配。** Python 默认日志级别是 WARNING，
    `log.info("开始处理任务 1")` 根本不会打印，你会以为后台什么都没发生。
   （`app/main.py` 里调了 `logging.basicConfig(level=logging.INFO)`。）
11. **`k` 太小，会让"能回答的问题"静默变成"查不到"。**
    实测：《三国演义》里问「曹操的别名是什么」，正确答案那一块在相似度
    **第 7~8 名** —— `k=5` 直接把它截掉，模型只能回"查不到"，
    而**日志里一点异常都没有**，看起来完全像系统坏了。
    排查手法：拿关键词（曹操的小名是"阿瞒"）去原文统计出现次数，
    先确认事实确实在库里，再加大 k 看排名。默认值已从 5 调到 10。
12. **展示用的名字别直接用存储用的文件名。**
    上传的文件在磁盘上是 `<sha12>_原名.txt`（防同名覆盖），而 `rag_cache`
    给块打的 `book` 标签就是文件 stem，于是引用来源里显示成
    `48922aa73c07_三国演义`。要在 `services/chat.py` 里映射回数据库的 `title` ——
    映射放在**读取侧**而不是写入侧，这样已经缓存好的旧数据也一起修好了。
13. **拒绝也要给信息。** 光说"我无法回答"对提问者没用。把
    「知识库里有哪些资料」塞进 prompt，模型才能说清
    "知识库里只有《三国演义》，没有关于孙悟空的内容" —— 这才是提问者要的答案。

---

## 12. 运行方式

```powershell
# 装依赖
pip install -r requirements.txt

# 配 Key（也可以复制 .env.example 成 .env）
$env:DEEPSEEK_API_KEY='sk-...'

# Web 版
python -m uvicorn app.main:app --reload
#   页面 http://127.0.0.1:8000/        首页
#        http://127.0.0.1:8000/admin   管理端
#        http://127.0.0.1:8000/docs    API 文档

# CLI 版（回归测试用）
python main.py
```
