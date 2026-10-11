#!/usr/bin/env bash
# ============================================================================
#  知识库问答系统 - Linux / macOS 启动脚本
#
#  用法：
#      chmod +x start.sh      # 只需执行一次
#      ./start.sh
#
#  它会做四件事：
#    1. 结束上一次遗留的 uvicorn 进程（--reload 的父子进程都要清）
#    2. 检查依赖，缺了就自动装
#    3. 提示 API Key 是否配好
#    4. 前台启动服务（Ctrl+C 停止）
#
#  想改端口：PORT=9000 ./start.sh  或直接改下面的默认值
# ============================================================================

# 不用 set -e：这里到处都需要"失败了也继续"的容错，用显式判断更清楚
set -uo pipefail

# ============================== 配置 ==============================
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
APP="app.main:app"
# ==================================================================

# 切到脚本所在目录（无论从哪里调用都能正确找到 app 包）
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# 只在真终端里输出颜色，重定向到文件时不掺控制字符
if [ -t 1 ]; then
  BOLD=$'\033[1m'; RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'
else
  BOLD=; RED=; GRN=; YEL=; RST=
fi

echo "${BOLD}============================================================${RST}"
echo "  知识库问答系统"
echo "------------------------------------------------------------"
echo "  工作目录 : $(pwd)"
echo "  首页     : http://${HOST}:${PORT}/"
echo "  管理端   : http://${HOST}:${PORT}/admin"
echo "  API 文档 : http://${HOST}:${PORT}/docs"
echo "${BOLD}============================================================${RST}"
echo

# ---------- 1. 找 python ----------
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "${RED}[错误]${RST} 找不到 python3，请先安装 Python 3.10+。"
  exit 1
fi

# ---------- 端口是否还被占用 ----------
port_in_use() {
  (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null
}

# ---------- 清理旧进程 ----------
cleanup_old() {
  local found=0

  # 第一步：按命令行匹配，结束 uvicorn 的父进程（reloader）。
  # 注意 pkill 只会杀掉父进程；子进程（spawn_main）会变成孤儿，
  # 并继续占着从父进程那里继承来的监听端口 —— 所以必须有第二步。
  if command -v pkill >/dev/null 2>&1; then
    if pkill -f "uvicorn .*${APP}" 2>/dev/null; then
      echo "       已结束旧的 uvicorn 进程"
      found=1
    fi
  fi

  # 第二步：按端口找。父进程死后，端口的持有者就是那个孤儿子进程
  # （子进程的命令行是 multiprocessing spawn_main，第一步匹配不到它）。
  local pids=""
  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null | sort -u)"
  elif command -v fuser >/dev/null 2>&1; then
    pids="$(fuser -n tcp "${PORT}" 2>/dev/null | tr -s '[:space:]' '\n' | grep -E '^[0-9]+$' | sort -u)"
  elif command -v ss >/dev/null 2>&1; then
    pids="$(ss -lptnH "sport = :${PORT}" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u)"
  fi

  local pid name
  for pid in $pids; do
    [ -n "$pid" ] || continue
    name="$(ps -p "$pid" -o comm= 2>/dev/null | tr -d ' ')"
    case "$name" in
      python*|uvicorn*|gunicorn*)
        echo "       结束占用端口 ${PORT} 的进程 PID=${pid} (${name})"
        kill -TERM "$pid" 2>/dev/null
        found=1
        ;;
      "")
        ;;  # 进程已经没了
      *)
        echo "       ${YEL}[警告]${RST} 端口 ${PORT} 被 ${name} 占用，已跳过（请手动处理）"
        ;;
    esac
  done

  # 等它们真正退出；赖着不走的强制结束
  if [ "$found" = 1 ]; then
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
      port_in_use || break
      sleep 0.3
    done
    for pid in $pids; do
      if kill -0 "$pid" 2>/dev/null; then
        kill -KILL "$pid" 2>/dev/null
      fi
    done
  fi

  if port_in_use; then
    echo "       ${RED}[错误]${RST} 端口 ${PORT} 仍被占用，请手动处理后重试。"
    return 1
  fi
  echo "       端口 ${PORT} 已空闲"
  return 0
}

# ---------- 2. 清理旧进程 ----------
echo "[1/3] 清理旧进程..."
cleanup_old || exit 1
echo

# ---------- 3. 检查依赖 ----------
if ! "$PY" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  echo "[2/3] 缺少依赖，正在安装 requirements.txt ..."
  if ! "$PY" -m pip install -r requirements.txt; then
    echo "${RED}[错误]${RST} 依赖安装失败，请手动执行：$PY -m pip install -r requirements.txt"
    exit 1
  fi
else
  echo "[2/3] 依赖检查通过"
fi
echo

# ---------- 4. 提示 API Key ----------
if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ ! -f .env ]; then
  echo "${YEL}[提示]${RST} 既没有 DEEPSEEK_API_KEY 环境变量，也没有 .env 文件。"
  echo "       问答会报鉴权失败，但上传入库不受影响。"
  echo "       可以把 .env.example 复制成 .env 再填 Key。"
  echo
fi

# ---------- 5. 启动 ----------
echo "[3/3] 启动服务中...（按 Ctrl+C 停止）"
echo
"$PY" -m uvicorn "$APP" --host "$HOST" --port "$PORT" --reload
RC=$?

echo
echo "服务已退出，返回码 = $RC"
exit "$RC"
