@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM ===========================================================================
REM  一键启动
REM
REM  为什么只有一个脚本、而旧版要三个：
REM    aq/api/app.py 的 mount_frontend() 会在 web/dist 存在时把前端挂到 "/"，
REM    也就是说**构建好的前端由 API 自己提供**，根本不需要第二个 Node 进程。
REM    于是"日常使用"完全可以退化成「双击一次 -> 起一个进程 -> 开浏览器」。
REM
REM  用法：
REM    启动.bat          生产模式（单进程，快；界面 = web/dist 构建产物）
REM    启动.bat dev      开发模式（API + Vite 双进程，改前端代码热更新）
REM    启动.bat rebuild  先重新构建前端，再按生产模式启动
REM
REM  旧脚本 初始化环境.bat / 启动API.bat / 启动前端.bat 仍然保留：
REM    初始化（装依赖）和"只要后端"（看 /docs）这两件事这里不做替代。
REM ===========================================================================

set "MODE=%~1"
set "PORT=8000"
set "URL=http://127.0.0.1:%PORT%"

echo ============================================================
echo   A 股 AI 量化交易系统 - 一键启动
echo   地址: %URL%
echo   文档: %URL%/docs
echo   停止: 在服务窗口按 Ctrl+C
echo ============================================================
echo.

REM ---------------------------------------------------------- 1) 找解释器
REM 目录存在 != 可用。必须探测 fastapi/uvicorn 真能 import，
REM 否则会以"代码坏了"的表象挂在 import 阶段。
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
    echo [错误] 没有可用的后端环境（需要 fastapi + uvicorn）。
    echo        正在尝试运行「初始化环境.bat」...
    echo.
    call "初始化环境.bat"
    echo.
    echo        初始化结束后请重新双击「启动.bat」。
    pause
    exit /b 1
)
echo [1/5] 后端环境就绪: !PY!

REM ---------------------------------------------------------- 2) 行情快照
REM 快照是全站数据的唯一来源。冷启动时它要扫全池（约 20~30 秒）；
REM 只要 data_cache/bars 没更新过，这里会直接复用缓存（不到 1 秒）。
echo.
echo [2/5] 检查行情快照 ...
"!PY!" -c "from aq.data.snapshot import MarketSnapshot as M; s=M(); fresh=s.is_fresh(); snap,meta=s.load(); print(('   [复用缓存]' if fresh else '   [已重建]')+' asof='+str(meta.get('asof'))+' 活跃='+str(meta.get('n_active'))+' 只 池='+str(meta.get('n_liquid_snapshot'))+' 只')"
if errorlevel 1 (
    echo   [警告] 快照构建失败，页面数值会为空。可稍后重试。
)

REM ---------------------------------------------------------- 3) 前端产物
set "NEED_DEV=0"
if /i "%MODE%"=="dev" set "NEED_DEV=1"

if /i "%MODE%"=="rebuild" (
    echo.
    echo [3/5] 重新构建前端 ...
    call :BUILD_WEB
    if errorlevel 1 goto :BUILD_FAIL
) else if "!NEED_DEV!"=="1" (
    echo.
    echo [3/5] 开发模式: 跳过构建，改用 Vite 热更新
) else (
    if not exist "web\dist\index.html" (
        echo.
        echo [3/5] 未找到 web\dist，正在构建前端 ...
        call :BUILD_WEB
        if errorlevel 1 goto :BUILD_FAIL
    ) else (
        echo.
        echo [3/5] 前端产物已存在（web\dist）。改了前端源码请用「启动.bat rebuild」重建。
    )
)

REM ---------------------------------------------------------- 4) 起后端
echo.
netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo [4/5] 端口 %PORT% 已被占用 —— **沿用已在运行的后端**，不新起进程。
    echo.
    echo       [!] 注意: 如果那个进程是改代码**之前**启动的，它跑的还是旧代码，
    echo           页面上看到的仍是旧行为。改过后端代码后请先关掉旧的
    echo           "AQ-API" 窗口（或按端口结束该进程）再运行本脚本。
    echo.
) else (
    echo [4/5] 启动后端（新窗口）...
    start "AQ-API" cmd /k "chcp 936 >nul && cd /d "%~dp0" && @echo off && echo === 后端 API (关闭此窗口即停止) === && "!PY!" -m uvicorn aq.api.app:app --host 127.0.0.1 --port %PORT%"
)

REM ---------------------------------------------------------- 5) 等健康检查
echo.
echo [5/5] 等待后端就绪 ...
set /a TRY=0
:WAIT
set /a TRY+=1
curl -s -o nul "%URL%/api/health" 2>nul
if not errorlevel 1 goto :READY
if !TRY! geq 40 goto :TIMEOUT
<nul set /p "=."
timeout /t 1 /nobreak >nul
goto :WAIT

:READY
echo.
echo   后端已就绪（等待 !TRY! 秒）。

if "!NEED_DEV!"=="1" (
    echo.
    echo   开发模式: 启动 Vite 开发服务器（新窗口）...
    start "AQ-WEB" cmd /k "chcp 936 >nul && cd /d "%~dp0web" && @echo off && echo === Vite 开发服务器 (关闭此窗口即停止) === && npm run dev"
    set "OPEN_URL=http://127.0.0.1:5173"
    set /a T2=0
    :WAIT2
    set /a T2+=1
    if !T2! gtr 30 goto :OPEN
    curl -s -o nul "http://127.0.0.1:5173" 2>nul
    if errorlevel 1 (
        timeout /t 1 /nobreak >nul
        goto :WAIT2
    )
) else (
    set "OPEN_URL=%URL%"
)

:OPEN
echo   打开浏览器: !OPEN_URL!
start "" "!OPEN_URL!"
echo.
echo ============================================================
echo   已启动。两个服务窗口（AQ-API / AQ-WEB）需保持开启。
echo   停止服务：直接关闭对应窗口，或按 Ctrl+C。
echo ============================================================
timeout /t 6 /nobreak >nul
endlocal
exit /b 0

:TIMEOUT
echo.
echo [错误] 等待 40 秒后端仍未就绪。
echo        请看名为 "AQ-API" 的窗口里的报错信息。
pause
endlocal
exit /b 1

:BUILD_FAIL
echo.
echo [错误] 前端构建失败。请先确认已安装 Node.js，并在 web 目录执行过 npm install。
echo        国内网络可先执行: npm config set registry https://registry.npmmirror.com
pause
endlocal
exit /b 1

REM ---------------------------------------------------------- 子过程
:BUILD_WEB
where node >nul 2>nul
if errorlevel 1 (
    echo   [错误] 未检测到 Node.js（需要 18 以上）。
    exit /b 1
)
pushd "web"
if not exist "node_modules\vite" (
    echo   正在安装前端依赖（首次较慢）...
    call npm install
    if errorlevel 1 (popd & exit /b 1)
)
call npm run build
set "BRC=!errorlevel!"
popd
exit /b !BRC!
