@echo off
chcp 65001 >nul
echo ============================================================
echo   启动前端开发服务器 (http://127.0.0.1:5173)
echo   请确保已先启动 API 服务
echo ============================================================
echo.

cd /d "%~dp0\web"

where node >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Node.js，请先安装 Node.js 18+
    pause
    exit /b 1
)

if not exist "node_modules" (
    echo [1/2] 首次运行，安装前端依赖 ...
    call npm install
    if errorlevel 1 (
        echo [错误] npm install 失败
        pause
        exit /b 1
    )
)

echo [2/2] 启动开发服务器 ...
call npm run dev
pause
