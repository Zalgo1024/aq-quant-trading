@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0\web"

echo ============================================================
echo   启动前端开发服务器
echo   地址: http://127.0.0.1:5173
echo   注意: 请先运行「启动API.bat」, 否则页面取不到数据
echo   停止: 按 Ctrl+C
echo ============================================================
echo.

REM ---- 检查 Node.js ----
where node >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Node.js, 请先安装 Node.js 18 以上版本。
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('node -v') do set "NODEVER=%%v"
echo [就绪] Node.js !NODEVER!

REM ---- 检查依赖 ----
REM node_modules 目录存在不代表装全了（中断的 npm install 会留下半成品）,
REM 所以直接检查 vite 入口文件。
echo.
if not exist "node_modules\vite" (
    echo [1/2] 安装前端依赖（首次运行, 需要几分钟）...
    call npm install
    if errorlevel 1 (
        echo.
        echo [错误] npm install 失败。
        echo        国内网络可先执行:
        echo          npm config set registry https://registry.npmmirror.com
        echo        然后重新运行本脚本。
        pause
        exit /b 1
    )
) else (
    echo [1/2] 依赖已就绪, 跳过安装
)

REM ---- 后端是否在线（只是提示, 不阻塞）----
echo.
netstat -ano ^| findstr ":8000" ^| findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo [2/2] 后端 API 已在运行, 启动 Vite ...
) else (
    echo [2/2] 警告: 端口 8000 没有响应, 后端 API 可能没启动。
    echo        页面能打开, 但数据接口会全部失败。
    echo        请先运行「启动API.bat」。
)
echo       页面地址 http://127.0.0.1:5173
echo.

call npm run dev

echo.
echo 前端已停止。
pause
endlocal
