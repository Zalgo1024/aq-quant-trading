@echo off
chcp 65001 >nul
echo ============================================================
echo   A 股 AI 量化交易系统 - 环境初始化
echo ============================================================
echo.

cd /d "%~dp0"

if not exist ".venv" (
    echo [1/3] 创建虚拟环境 .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败，请确认已安装 Python 3.10+
        pause
        exit /b 1
    )
) else (
    echo [1/3] 虚拟环境已存在，跳过
)

echo [2/3] 安装后端依赖 ...
call .venv\Scripts\python.exe -m pip install --upgrade pip -q -i https://pypi.tuna.tsinghua.edu.cn/simple
call .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo [警告] 清华源安装失败，改用默认源重试 ...
    call .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败
        pause
        exit /b 1
    )
)

echo [3/3] 运行冒烟测试 ...
call .venv\Scripts\python.exe -m aq.smoke_test
if errorlevel 1 (
    echo.
    echo [警告] 冒烟测试存在失败项，请检查上面的输出
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   初始化完成！
echo   启动 API:  启动API.bat
echo   启动前端:  启动前端.bat
echo ============================================================
pause
