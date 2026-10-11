@echo off
chcp 65001 >nul 2>&1
REM ============================================================================
REM  知识库问答系统 - Windows 启动脚本
REM
REM  用法：双击本文件，或在命令行执行  start.bat
REM
REM  它会做四件事：
REM    1. 结束上一次遗留的 uvicorn 进程（--reload 的父子进程都要清）
REM    2. 检查依赖，缺了就自动装
REM    3. 提示 API Key 是否配好
REM    4. 前台启动服务（Ctrl+C 停止）
REM
REM  想改端口：改下面的 PORT 即可。
REM
REM  【改本文件时必须遵守的两个约束，否则会很难排查】
REM
REM   1) 行尾必须是 CRLF，不能是 LF。
REM      cmd.exe 遇到 LF 行尾的批处理会解析错乱 —— 它会把上一条错误信息切碎
REM      当成命令去执行，报一堆 "xxx is not recognized"，现象极具误导性。
REM      （用 git 提交时注意 core.autocrlf，别让它把行尾改成 LF。）
REM
REM   2) chcp 65001 必须位于任何中文之前，也就是 @echo off 的下一行。
REM      cmd.exe 是逐行用「当前代码页」解析批处理文件的，而控制台默认是 936。
REM      实测：UTF-8 文件若不先 chcp，部分汉字会被输出成 '?'。
REM
REM   文件编码保持 UTF-8（无 BOM）即可，不要转成 GBK。
REM ============================================================================

setlocal EnableExtensions

REM ---------- 切到脚本所在目录（双击时工作目录不一定是这里）----------
cd /d "%~dp0"

REM ============================== 配置 ==============================
set "HOST=127.0.0.1"
set "PORT=8000"
set "APP=app.main:app"
REM ==================================================================

title 知识库问答系统 - 端口 %PORT%

echo ============================================================
echo   知识库问答系统
echo ------------------------------------------------------------
echo   工作目录 : %CD%
echo   首页     : http://%HOST%:%PORT%/
echo   管理端   : http://%HOST%:%PORT%/admin
echo   API 文档 : http://%HOST%:%PORT%/docs
echo ============================================================
echo.

REM ---------- 1. 检查 python ----------
where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 找不到 python 命令。请确认 Python 已安装并加入 PATH。
    goto :end
)

REM ---------- 2. 清理旧进程 ----------
echo [1/3] 清理旧进程...
call :kill_old
ping -n 2 127.0.0.1 >nul
echo.

REM ---------- 3. 检查依赖 ----------
python -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
    echo [2/3] 缺少依赖，正在安装 requirements.txt ...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请手动执行：python -m pip install -r requirements.txt
        goto :end
    )
) else (
    echo [2/3] 依赖检查通过
)
echo.

REM ---------- 4. 提示 API Key ----------
if not defined DEEPSEEK_API_KEY if not exist ".env" (
    echo [提示] 既没有 DEEPSEEK_API_KEY 环境变量，也没有 .env 文件。
    echo        问答会报鉴权失败，但上传入库不受影响。
    echo        可以把 .env.example 复制成 .env 再填 Key。
    echo.
)

REM ---------- 5. 启动 ----------
echo [3/3] 启动服务中...（按 Ctrl+C 停止）
echo.
python -m uvicorn %APP% --host %HOST% --port %PORT% --reload
set "RC=%ERRORLEVEL%"

echo.
echo 服务已退出，返回码 = %RC%

:end
echo.
pause
exit /b 0


REM ============================================================================
REM  子过程：清理旧的 uvicorn 进程
REM
REM  为什么要分两步清理：
REM    uvicorn --reload 会分出两个进程（实测）：
REM      - 父进程 reloader：负责 bind 端口、监视文件、重启子进程。
REM                        它是**端口的实际持有者**（uvicorn 源码里先 bind_socket 再 spawn）。
REM      - 子进程 server  ：才真正 import app.main、跑 ASGI 应用，
REM                        我们的后台 Worker 线程也在它里面。
REM    两边都要清：
REM      只杀子进程 -> 父进程立刻把它重新拉起来
REM      只杀父进程 -> 子进程变孤儿，继续占着继承来的端口
REM    所以第一步按命令行匹配父进程并用 /T 连子进程一起结束；
REM    第二步用端口兜底，防止有漏网的。
REM ============================================================================
:kill_old

REM --- 第一步：按命令行精确匹配 uvicorn 主进程；/T 连子进程一起结束 ---
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*uvicorn*%APP%*' } | ForEach-Object { Write-Host ('       结束 uvicorn 进程 PID=' + $_.ProcessId); Start-Process -FilePath 'taskkill' -ArgumentList '/F','/T','/PID',$_.ProcessId -NoNewWindow -Wait -ErrorAction SilentlyContinue }"

REM --- 第二步：兜底检查端口。只杀 python，别人的程序绝不动 ---
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue; if (-not $c) { Write-Host '       端口 %PORT% 已空闲'; exit }; $c.OwningProcess | Sort-Object -Unique | ForEach-Object { $p = Get-Process -Id $_ -ErrorAction SilentlyContinue; if ($p -and $p.ProcessName -eq 'python') { Write-Host ('       结束占用端口 %PORT% 的 python PID=' + $_); Start-Process -FilePath 'taskkill' -ArgumentList '/F','/T','/PID',$_ -NoNewWindow -Wait -ErrorAction SilentlyContinue } elseif ($p) { Write-Host ('       [警告] 端口 %PORT% 被 ' + $p.ProcessName + ' 占用，已跳过') } }"

goto :eof
