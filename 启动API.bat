@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   启动后端 API 服务
echo   地址: http://127.0.0.1:8000
echo   文档: http://127.0.0.1:8000/docs
echo   停止: 按 Ctrl+C
echo ============================================================
echo.

REM ---- 选解释器: 优先 .venv, 回退系统 python ----
REM 和初始化脚本一样: 目录存在不等于可用, 要探测 fastapi/uvicorn 真的能 import。
set "PY="
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import fastapi,uvicorn" >nul 2>nul
    if not errorlevel 1 set "PY=.venv\Scripts\python.exe"
)
if not defined PY (
    python -c "import fastapi,uvicorn" >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [错误] 没有可用的 Python 环境（需要 fastapi + uvicorn）。
    echo        请先运行「初始化环境.bat」。
    pause
    exit /b 1
)
echo [就绪] 使用 !PY!

REM ---- 端口占用检查 ----
REM 没有这一步的话, 重复双击会报一堆 "address already in use",
REM 看起来像服务崩了, 实际只是已经有一个在跑。
netstat -ano ^| findstr ":8000" ^| findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo.
    echo [提示] 端口 8000 已被占用, API 可能已经在运行:
    echo        http://127.0.0.1:8000/docs
    pause
    exit /b 1
)

echo.
echo 正在启动 ... 浏览器打开 http://127.0.0.1:8000/docs
echo.
"!PY!" -m uvicorn aq.api.app:app --host 127.0.0.1 --port 8000

echo.
echo API 已停止。
pause
endlocal
