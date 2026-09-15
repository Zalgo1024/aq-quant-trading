@echo off
chcp 65001 >nul
echo ============================================================
echo   启动 API 服务 (http://127.0.0.1:8000)
echo   API 文档: http://127.0.0.1:8000/docs
echo   按 Ctrl+C 停止
echo ============================================================
echo.

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到虚拟环境，请先运行「初始化环境.bat」
    pause
    exit /b 1
)

call .venv\Scripts\python.exe -m uvicorn aq.api.app:app --host 127.0.0.1 --port 8000 --reload
pause
