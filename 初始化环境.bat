@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   A 股 AI 量化交易系统 - 环境初始化
echo ============================================================
echo.

REM ---------------------------------------------------------- 1) 找 Python
echo [1/4] 检查 Python ...
set "SYS_PY=python"
where python >nul 2>nul
if errorlevel 1 (
    set "SYS_PY=py -3"
    where py >nul 2>nul
    if errorlevel 1 (
        echo   [错误] 未检测到 Python。
        echo        请先安装 Python 3.10 以上版本, 并勾选添加到 PATH。
        pause
        exit /b 1
    )
)
REM 注意: 这里不能写 "sys.version_info >= (3,10)" 这类判断。
REM cmd 在双引号内仍把 > 当重定向, 命令被截断后 errorlevel 非 0,
REM 于是明明版本够也会误报"版本过低"。改成导入检测 + 直接打印版本。
%SYS_PY% -c "import sys, venv" >nul 2>nul
if errorlevel 1 (
    echo   [错误] 当前 Python 无法创建虚拟环境, 需要 3.10 以上版本。
    pause
    exit /b 1
)
echo        使用 %SYS_PY%
%SYS_PY% --version
echo        项目要求 3.10 以上; 若版本不符请升级后重新运行。

REM ---------------------------------------------------------- 2) 建 venv
echo.
echo [2/4] 准备虚拟环境 .venv ...
REM 关键: 目录存在不等于可用。这里曾经踩过: .venv 是个只有 pip/setuptools 的
REM "空壳", pandas/fastapi/uvicorn 一个都没有, 于是三个启动脚本全部挂在
REM import 上, 而报错看起来却像"代码坏了"。所以必须探测依赖, 不能只看目录。
set "VENV_OK=0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import pandas, fastapi, uvicorn" >nul 2>nul
    if not errorlevel 1 set "VENV_OK=1"
)
if "!VENV_OK!"=="1" (
    echo        .venv 已就绪, 跳过创建
) else (
    if exist ".venv" (
        echo        现有 .venv 缺少核心依赖, 正在重建 ...
        rmdir /s /q ".venv"
    ) else (
        echo        首次运行, 正在创建 ...
    )
    REM --system-site-packages: 继承系统里已装好的 numpy/pandas/fastapi/uvicorn,
    REM 初始化不必联网重下一遍, 离线环境也能跑通。
    %SYS_PY% -m venv .venv --system-site-packages
    if errorlevel 1 (
        echo   [错误] 创建虚拟环境失败。
        pause
        exit /b 1
    )
    echo        已创建（继承系统已装依赖, 离线也能用）
)

REM ---------------------------------------------------------- 3) 装依赖
echo.
echo [3/4] 检查后端依赖 ...
".venv\Scripts\python.exe" -c "import pandas, fastapi, uvicorn, pyarrow, pydantic, yaml" >nul 2>nul
if not errorlevel 1 (
    echo        核心依赖已具备, 跳过安装
) else (
    echo        正在安装 requirements.txt ...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo        清华源失败, 改用默认源重试 ...
        ".venv\Scripts\python.exe" -m pip install -r requirements.txt
        if errorlevel 1 (
            echo   [错误] 依赖安装失败, 请检查网络后重新运行本脚本。
            pause
            exit /b 1
        )
    )
)

REM ---------------------------------------------------------- 4) 冒烟测试
echo.
echo [4/4] 冒烟测试
set "RUNSMOKE=Y"
set /p "RUNSMOKE=        是否运行（校验全部子系统, 较慢）? [Y/n] "
if /i "!RUNSMOKE!"=="n" (
    echo        已跳过
) else (
    ".venv\Scripts\python.exe" -m aq.smoke_test
    if errorlevel 1 (
        echo   [警告] 冒烟测试存在失败项, 请看上面的输出。
    ) else (
        echo        冒烟测试全部通过
    )
)

echo.
echo ============================================================
echo   初始化完成
echo.
echo     日常使用:  直接双击「启动.bat」
echo                （它会一次性起好后端并打开浏览器；
echo                  已构建的前端由后端直接提供，不需要第二个窗口）
echo.
echo     调试前端:  启动.bat dev     改前端源码时用（Vite 热更新）
echo     只要后端:  启动API.bat      只跑 API，看 /docs 接口文档
echo.
echo     后端文档:  http://127.0.0.1:8000/docs
echo ============================================================
pause
endlocal
