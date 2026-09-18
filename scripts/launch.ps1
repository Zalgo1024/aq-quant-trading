<#
================================================================================
 一键启动 · 真正的逻辑（根目录的 启动.bat 只是 ASCII 转发壳）

 为什么要分成 .bat + .ps1 两层（不是过度设计，是被坑出来的）：
   1. cmd 解析 .bat 时按**当前代码页**逐字节读文件。UTF-8 的中文注释被当成 GBK 后
      字节错位，会吞掉文件末尾的字节，进而把命令切碎（`timeout` 变 `meout`、
      `if not exist` 变 `exist`），报一堆"不是内部或外部命令"。所以 .bat 里
      **一个非 ASCII 字符都不能有**。
   2. 旧版 .bat 是 LF 换行（Write 工具的默认）。cmd 对 LF-only 的批处理解析会错乱，
      实测同一个脚本：CRLF 版 8 行输出全部正确，LF 版只剩 4 行且命令被切碎。
      → .bat 必须 CRLF。
   3. 旧版用 `start "T" cmd /k "... "%~dp0web" ..."` 嵌套引号：内层引号提前闭合外层，
      于是 `"D:\...\web" && @echo off && ...` 被当成命令执行 —— 正是你截图里那两行报错。
      → 这里改用 PowerShell 的参数数组传参（不经过 shell 字符串），从根上避免。
   4. 中文提示放在本文件里，并以 **UTF-8 with BOM** 保存，PowerShell 5.1 才能正确读取。
      PowerShell 输出中文走 Unicode 控制台 API，与控制台代码页无关。

 用法（等价于 double-click 后的参数）：
   启动.bat              生产模式：单窗口，本窗口就是服务窗口，Ctrl+C 即停
   启动.bat dev          开发模式：API + Vite 两个新窗口，前端热更新
   启动.bat rebuild      先强制重建前端产物，再按生产模式启动
   启动.bat setup        安装/更新依赖（后端 pip + 前端 npm）
   启动.bat check        只做体检并打印报告，不启动任何服务

 可选开关：
   -Port 8001   换端口
   -NoBrowser   不自动打开浏览器
   -NoRestart   端口上已有实例时不重启（默认发现旧构建会自动重启）
   -SkipBuild   前端产物比源码旧时也不自动重建（默认会自动重建）
================================================================================
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('prod', 'dev', 'rebuild', 'setup', 'check')]
    [string]$Mode = 'prod',

    [int]$Port = 8000,
    [switch]$NoBrowser,
    [switch]$NoRestart,
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'

# ----------------------------------------------------------------- 输出小工具
# 中文一律由 PowerShell 自己打印（Unicode 控制台 API），不依赖控制台代码页；
# 凡是要被脚本读取的原生命令输出，一律要求其为纯 ASCII（见 Invoke-Py 的约定）。
function Write-Head([string]$Text) {
    Write-Host ''
    Write-Host '============================================================' -ForegroundColor DarkCyan
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host '============================================================' -ForegroundColor DarkCyan
}
function Write-Step([int]$N, [int]$Total, [string]$Text) {
    Write-Host ''
    Write-Host ("[{0}/{1}] " -f $N, $Total) -ForegroundColor White -NoNewline
    Write-Host $Text
}
function Write-Ok([string]$Text)   { Write-Host '   OK  ' -ForegroundColor Green  -NoNewline; Write-Host $Text }
function Write-Note([string]$Text) { Write-Host '   --  ' -ForegroundColor DarkGray -NoNewline; Write-Host $Text }
function Write-Warn2([string]$Text){ Write-Host '   !!  ' -ForegroundColor Yellow -NoNewline; Write-Host $Text }
function Write-Bad([string]$Text)  { Write-Host '   XX  ' -ForegroundColor Red    -NoNewline; Write-Host $Text }

# 双击运行时窗口会随脚本结束而消失，所以要留一口气给用户读完。
# 非交互（管道重定向 / 自动化调用）时直接返回，避免卡住调用方。
function Wait-ForCloseIfInteractive([string]$Prompt) {
    if ([Console]::IsInputRedirected) { return }
    Write-Host ''
    [void](Read-Host $Prompt)
}

# ----------------------------------------------------------------- 工程根定位
function Resolve-ProjectRoot {
    # 与项目约定一致：向上找 config/base.yaml，而不是硬编码 parents[1]
    $d = $PSScriptRoot
    while ($d) {
        if (Test-Path (Join-Path $d 'config\base.yaml')) { return $d }
        $p = Split-Path -Parent $d
        if (-not $p -or $p -eq $d) { break }
        $d = $p
    }
    return (Split-Path -Parent $PSScriptRoot)
}

$Root = Resolve-ProjectRoot
Set-Location $Root

# ----------------------------------------------------------------- 原生调用
# 约定：凡是要被解析的 Python 输出，只允许打印 ASCII（形如 SNAP|CACHE|2026-09-15|5548|5065）。
# 这样无论控制台代码页是 936 / 65001 / 437，读取都不会乱码。
function Invoke-PyRaw {
    param([string]$Interpreter, [string]$Code)
    $out = & $Interpreter -c $Code 2>&1
    return @{ Exit = $LASTEXITCODE; Text = ($out | Out-String) }
}

function Test-PythonInterpreter {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if ($Candidate -match '[\\/]' -and -not (Test-Path $Candidate)) { return $false }
    try {
        & $Candidate -c "import fastapi, uvicorn" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Resolve-BackendPython {
    # 目录存在 != 可用：必须真能 import fastapi/uvicorn，否则会以"代码坏了"的表象挂在启动阶段
    $venv = Join-Path $Root '.venv\Scripts\python.exe'
    foreach ($cand in @($venv, 'python', 'py')) {
        if ($cand -eq $venv -and -not (Test-Path $venv)) { continue }
        if (Test-PythonInterpreter -Candidate $cand) { return $cand }
    }
    return $null
}

function Test-NodeAvailable {
    try { & node -v *> $null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}

# ----------------------------------------------------------------- 运行态探测
function Get-RunningInstance {
    param([int]$P)
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$P/api/health" -TimeoutSec 3
    } catch {
        return $null
    }
    if (-not $r) { return $null }
    # 只认"我们自己的服务"：必须有 status + data_source 两个字段
    $names = @($r.PSObject.Properties.Name)
    if (($names -contains 'status') -and ($names -contains 'data_source')) { return $r }
    return $null
}

# 判定"端口上跑的是不是最新代码"。
# 严格策略：**任何认不出来的形状都判为旧**。宁可多重启一次，也不能把旧代码当新代码
# 留下来（假阴性比假阳性危险得多 —— 那正是"改了没变化"的来源）。
function Get-InstanceStaleness {
    param($Instance)
    if (-not $Instance.code) {
        return '对端没有上报代码身份（该进程是加这个字段之前的旧版本）'
    }
    $names = @($Instance.code.PSObject.Properties.Name)
    if (-not ($names -contains 'backend_stale')) {
        return ('对端上报的是旧版身份字段（{0}），说明它启动时还没有最新改动' -f ($names -join ','))
    }
    if ($Instance.code.backend_stale) {
        return ('后端源码在进程启动之后被改过：进程启动于 {0}，源码最后修改 {1}' -f $Instance.code.boot.boot_time, $Instance.code.disk.backend_mtime)
    }
    if (-not $Instance.code.boot.boot_time) {
        return '对端没有上报启动时间，无法确认它加载的是最新代码'
    }
    return $null
}

function Get-PortOwnerPid {
    param([int]$P)
    try {
        $c = Get-NetTCPConnection -LocalPort $P -State Listen -ErrorAction Stop
        return @($c | Select-Object -ExpandProperty OwningProcess -Unique)
    } catch {
        return @()
    }
}

function Get-LocalDistAsset {
    $idx = Join-Path $Root 'web\dist\index.html'
    if (-not (Test-Path $idx)) { return $null }
    $txt = Get-Content -Raw -Encoding UTF8 $idx
    foreach ($tok in $txt.Split('"')) {
        if ($tok -like '/assets/*.js') { return (Split-Path -Leaf $tok) }
    }
    return $null
}

function Get-NewestSourceTime {
    $targets = @()
    foreach ($rel in @('web\src', 'web\index.html', 'web\package.json', 'web\vite.config.ts', 'web\vite.config.js')) {
        $p = Join-Path $Root $rel
        if (Test-Path $p) { $targets += Get-Item $p }
    }
    $src = Join-Path $Root 'web\src'
    if (Test-Path $src) { $targets += Get-ChildItem -Path $src -Recurse -File -ErrorAction SilentlyContinue }
    if (-not $targets) { return $null }
    return ($targets | Sort-Object LastWriteTime -Descending | Select-Object -First 1)
}

# ----------------------------------------------------------------- 前端构建
function Invoke-WebBuild {
    param([switch]$InstallDeps)
    if (-not (Test-NodeAvailable)) {
        Write-Bad '未检测到 Node.js（需要 18 以上）。装好后重试，或用 启动.bat setup。'
        return $false
    }
    $web = Join-Path $Root 'web'
    Push-Location $web
    try {
        if ($InstallDeps -or -not (Test-Path 'node_modules\vite')) {
            Write-Note '安装前端依赖（首次较慢）...'
            & npm install
            if ($LASTEXITCODE -ne 0) { Write-Bad 'npm install 失败。'; return $false }
        }
        Write-Note 'npm run build ...'
        & npm run build
        if ($LASTEXITCODE -ne 0) { Write-Bad 'npm run build 失败（上面有详细报错）。'; return $false }
        return $true
    } finally {
        Pop-Location
    }
}

# ----------------------------------------------------------------- 浏览器
function Open-WhenReady {
    param([string]$Url, [string]$HealthUrl)
    $job = Start-Job -ArgumentList $Url, $HealthUrl -ScriptBlock {
        param($u, $h)
        for ($i = 0; $i -lt 90; $i++) {
            try { Invoke-WebRequest -Uri $h -UseBasicParsing -TimeoutSec 2 | Out-Null; break }
            catch { Start-Sleep -Milliseconds 500 }
        }
        Start-Process $u
    }
    return $job
}

# ================================================================= 主流程
$script:Failed = $false

Write-Head ("A 股 AI 量化交易系统  ·  启动模式: {0}" -f $Mode)
Write-Note ("工程根: {0}" -f $Root)
Write-Note ("地址  : http://127.0.0.1:{0}   (接口文档 /docs)" -f $Port)

# ---- setup：装环境。
# 必须排在最前面 —— 全新克隆时还没有可用的后端解释器，后面的检查会直接判失败。
if ($Mode -eq 'setup') {
    $steps = 4
    Write-Head '安装 / 更新依赖（setup）'

    $venvPy = Join-Path $Root '.venv\Scripts\python.exe'
    $needVenv = $true
    if (Test-Path $venvPy) {
        & $venvPy -c "import pandas, fastapi, uvicorn" *> $null
        if ($LASTEXITCODE -eq 0) {
            $needVenv = $false
            Write-Ok '.venv 已就绪，跳过创建。'
        } else {
            Write-Warn2 '.venv 存在但缺核心依赖，将直接往里补装（不删目录）。'
        }
    }

    if ($needVenv) {
        $sysPy = $null
        foreach ($c in @('python', 'py')) {
            try {
                & $c -c "import sys, venv" *> $null
                if ($LASTEXITCODE -eq 0) { $sysPy = $c; break }
            } catch { }
        }
        if (-not $sysPy) {
            Write-Bad '系统 Python 无法创建虚拟环境（需要 3.10 以上）。'
            exit 1
        }
        Write-Step 1 $steps ('创建虚拟环境 .venv（解释器 {0}）...' -f $sysPy)
        # --system-site-packages：继承系统里已装好的 numpy/pandas/fastapi，
        # 离线环境也能跑通，不必联网重下一遍。
        & $sysPy -m venv (Join-Path $Root '.venv') --system-site-packages
        if ($LASTEXITCODE -ne 0) { Write-Bad '创建虚拟环境失败。'; exit 1 }
        Write-Ok '虚拟环境已创建。'
    }

    Write-Step 2 $steps '安装后端依赖 ...'
    $req = Join-Path $Root 'requirements.txt'
    & $venvPy -m pip install -r $req -i https://pypi.tuna.tsinghua.edu.cn/simple
    if ($LASTEXITCODE -ne 0) {
        Write-Note '清华源失败，改用默认源重试。'
        & $venvPy -m pip install -r $req
        if ($LASTEXITCODE -ne 0) { Write-Bad '后端依赖安装失败。'; $script:Failed = $true }
    }
    & $venvPy -c "import pandas, fastapi, uvicorn, pyarrow, pydantic, yaml" *> $null
    if ($LASTEXITCODE -eq 0) { Write-Ok '后端依赖已就绪。' }
    else { Write-Bad '后端依赖仍不齐（见上面 pip 输出）。'; $script:Failed = $true }

    Write-Step 3 $steps '安装前端依赖并构建 ...'
    if (Invoke-WebBuild -InstallDeps) { Write-Ok '前端依赖已就绪并完成一次构建。' }
    else { $script:Failed = $true }

    Write-Step 4 $steps '冒烟自测 ...'
    & $venvPy -m aq.smoke_test
    if ($LASTEXITCODE -ne 0) { Write-Warn2 '冒烟测试有失败项（见上面输出）。' }
    else { Write-Ok '冒烟测试通过。' }

    if ($script:Failed) {
        Write-Bad '有步骤失败，请按上面的提示处理后重试。'
        exit 1
    }
    Write-Ok '依赖就绪。现在可以直接双击 启动.bat。'
    Wait-ForCloseIfInteractive '按回车键关闭本窗口'
    exit 0
}

# ---- 1) 后端解释器
Write-Step 1 5 '检查后端环境 ...'
$Py = Resolve-BackendPython
if (-not $Py) {
    Write-Bad '没有可用的后端环境（需要 fastapi + uvicorn）。'
    Write-Note '先执行: 启动.bat setup  （安装依赖）'
    Write-Note '若 .venv 已建好但仍报错，请检查 .venv\Scripts\python.exe 能否 import fastapi。'
    $script:Failed = $true
} else {
    Write-Ok ("后端解释器: {0}" -f $Py)
}

if ($script:Failed) { exit 1 }

# ---- 2) 行情快照（全站数据唯一来源）
Write-Step 2 5 '检查行情快照 ...'
$snapCode = "from aq.data.snapshot import MarketSnapshot as M; s=M(); f=s.is_fresh(); snap,meta=s.load(); print('SNAP|'+('CACHE' if f else 'BUILT')+'|'+str(meta.get('asof'))+'|'+str(meta.get('n_active'))+'|'+str(meta.get('n_liquid_snapshot')))"
$snap = Invoke-PyRaw -Interpreter $Py -Code $snapCode
$snapLine = @($snap.Text -split "`r?`n" | Where-Object { $_ -like 'SNAP|*' })
if ($snapLine.Count -eq 0) {
    Write-Warn2 '快照构建失败（页面数值会为空）。单独排查：'
    Write-Note ($snap.Text.Trim())
} else {
    $f = $snapLine[0].Split('|')
    $how = '复用缓存'
    if ($f[1] -eq 'BUILT') { $how = '已重建' }
    Write-Ok ("行情快照[{0}]  数据截至 {1}  活跃 {2} 只  流动性池 {3} 只" -f $how, $f[2], $f[3], $f[4])
}

# ---- 4) 前端产物
Write-Step 3 5 '检查前端产物 ...'
$distIndex = Join-Path $Root 'web\dist\index.html'
$needBuild = $false
$reason = ''
if ($Mode -eq 'rebuild') { $needBuild = $true; $reason = 'rebuild 模式要求重建' }
elseif (-not (Test-Path $distIndex)) { $needBuild = $true; $reason = 'web\dist 不存在' }
else {
    $newest = Get-NewestSourceTime
    if ($newest) {
        $distTime = (Get-Item $distIndex).LastWriteTime
        if ($newest.LastWriteTime -gt $distTime) {
            $reason = ("源码 {0} 比产物 {1} 新" -f $newest.LastWriteTime.ToString('MM-dd HH:mm'), $distTime.ToString('MM-dd HH:mm'))
            $needBuild = $true
        }
    }
}

if ($Mode -eq 'dev') {
    Write-Note '开发模式：前端由 Vite 提供（热更新），不检查 web\dist。'
} elseif ($needBuild) {
    if ($Mode -eq 'check') {
        Write-Warn2 ("{0}。check 模式只报告、不重建；要重建请执行 启动.bat rebuild。" -f $reason)
    } elseif ($SkipBuild) {
        Write-Warn2 ("{0}，但已指定 -SkipBuild：继续用旧产物（界面可能是旧版）。" -f $reason)
    } else {
        Write-Warn2 ("{0}，正在自动重建（约 20~60 秒）..." -f $reason)
        if (Invoke-WebBuild) {
            Write-Ok ("重建完成，产物: {0}" -f (Get-LocalDistAsset))
        } else {
            Write-Bad '前端构建失败。可用 启动.bat setup 重装依赖后重试。'
            $script:Failed = $true
        }
    }
} else {
    Write-Ok ("产物已是最新: {0}" -f (Get-LocalDistAsset))
}

if ($script:Failed) { exit 1 }

# ---- 5) 端口 / 已有实例
Write-Step 4 5 '检查端口占用 ...'
$Url = "http://127.0.0.1:$Port"
# check 模式是只读体检：绝不停止任何进程、绝不重建，只报告。
$noRestartEffective = ($NoRestart -or ($Mode -eq 'check'))
$inst = Get-RunningInstance -P $Port
$instanceFresh = $false
if ($inst) {
    # 判定「端口上跑的是不是最新代码」：比的是**进程启动时的快照**，不是磁盘现值。
    # （磁盘现值对任何进程都一样，拿它比对永远相等，比不出来 —— 第一版就错在这里。）
    $staleReason = Get-InstanceStaleness -Instance $inst

    if (-not $staleReason) {
        Write-Ok ("端口 {0} 已有本服务在运行，且加载的是最新后端代码（启动于 {1}）。" -f $Port, $inst.code.boot.boot_time)
        Write-Note ("数据截至 {0}；快照构建于 {1}" -f $inst.data_asof, $inst.snapshot_built_at)
        $instanceFresh = $true
    } elseif ($noRestartEffective) {
        Write-Warn2 ("端口 {0} 上跑的是**旧代码**：{1}" -f $Port, $staleReason)
        Write-Note '打开页面会看到旧行为。要生效：直接跑 启动.bat（它会自动重启）。'
    } else {
        Write-Warn2 ("端口 {0} 上跑的是**旧代码**：{1}" -f $Port, $staleReason)
        Write-Note '正在停止它并重新启动 —— 这正是「改了代码却看不出变化」最常见的原因。'
        $pids = Get-PortOwnerPid -P $Port
        foreach ($procId in $pids) {
            try {
                $p = Get-Process -Id $procId -ErrorAction Stop
                Stop-Process -Id $procId -Force -ErrorAction Stop
                Write-Ok ("已停止旧进程 pid={0} ({1})" -f $procId, $p.ProcessName)
            } catch {
                Write-Bad ("无法停止 pid={0}: {1}" -f $procId, $_.Exception.Message)
                $script:Failed = $true
            }
        }
        if ($script:Failed) {
            Write-Note ('请手动结束占用 {0} 端口的进程后重试。' -f $Port)
            exit 1
        }
        Start-Sleep -Milliseconds 800
    }
} else {
    $pids = Get-PortOwnerPid -P $Port
    if ($pids.Count -gt 0) {
        Write-Warn2 ("端口 {0} 被其它进程占用（pid {1}），但它不是本服务。" -f $Port, ($pids -join ', '))
        Write-Note ("改用其它端口: 启动.bat check -Port 8001")
        exit 1
    }
    Write-Ok ("端口 {0} 空闲" -f $Port)
}

if ($Mode -eq 'check') {
    Write-Step 5 5 '体检完成（check 模式不启动服务、不停止任何进程）。'
    Write-Note '可用模式: prod(默认) / dev / rebuild / setup / check'
    Wait-ForCloseIfInteractive '按回车键关闭本窗口'
    exit 0
}

if ($instanceFresh) {
    # 已有同版本实例在跑：绝不起第二个进程，只把界面带出来。
    if (-not $NoBrowser) {
        Start-Process $Url
        Write-Note ("已打开浏览器: {0}" -f $Url)
    }
    Write-Head '服务已在运行，本次不重复启动'
    Write-Note ("界面: {0}" -f $Url)
    Write-Note ("文档: {0}/docs" -f $Url)
    Write-Note '要停止它，请到那个正在运行服务的窗口按 Ctrl+C。'
    Wait-ForCloseIfInteractive '按回车键关闭本窗口（不会停止已在运行的服务）'
    exit 0
}

# ---- 6) 启动
Write-Step 5 5 '启动服务 ...'

if ($Mode -eq 'dev') {
    Write-Note 'API 与 Vite 将各占一个窗口，关掉这两个窗口即停止服务。'
    Start-Process -FilePath $Py -WorkingDirectory $Root -ArgumentList @(
        '-u', '-m', 'uvicorn', 'aq.api.app:app', '--host', '127.0.0.1', '--port', "$Port", '--reload'
    )
    Start-Process -FilePath 'cmd.exe' -WorkingDirectory (Join-Path $Root 'web') -ArgumentList @('/k', 'npm run dev')

    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
        if (Get-RunningInstance -P $Port) { $ok = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if ($ok) { Write-Ok '后端已就绪。' } else { Write-Warn2 '60 秒内后端未就绪，请看新开的 API 窗口。' }

    $devUrl = 'http://127.0.0.1:5173'
    if (-not $NoBrowser) { Start-Process $devUrl }
    Write-Head '已启动（开发模式）'
    Write-Note ("界面: {0}   （Vite 热更新）" -f $devUrl)
    Write-Note ("接口: {0}/docs" -f $Url)
    Write-Note '停止: 关闭 AQ 的两个窗口（uvicorn / npm）'
    exit 0
}

# 生产模式：服务就运行在**当前窗口**，Ctrl+C 或关窗口即停
if (-not $NoBrowser) {
    $job = Open-WhenReady -Url $Url -HealthUrl ("{0}/api/health" -f $Url)
    Write-Note '已安排在服务就绪后自动打开浏览器。'
}
Write-Host ''
Write-Host '------------------------------------------------------------' -ForegroundColor DarkCyan
Write-Host ("  界面: {0}" -f $Url)
Write-Host ("  文档: {0}/docs" -f $Url)
Write-Host '  停止: 在本窗口按 Ctrl+C，或直接关闭本窗口'
Write-Host '------------------------------------------------------------' -ForegroundColor DarkCyan
Write-Host ''

try {
    & $Py -u -m uvicorn aq.api.app:app --host 127.0.0.1 --port $Port
} finally {
    if ($job) { Stop-Job $job -ErrorAction SilentlyContinue; Remove-Job $job -Force -ErrorAction SilentlyContinue }
    Write-Host ''
    Write-Host '服务已停止。' -ForegroundColor DarkGray
}
