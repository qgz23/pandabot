# ============================================================
#  PandaBot 更新脚本（Windows Server）
#
#  作用：拉最新代码 -> 补依赖 -> 重启进程 -> 看日志确认真的起来了
#
#  三种代码来源，脚本自动判断：
#    1. 目录是 git 仓库            -> git pull --ff-only
#    2. 传了 -Zip 离线包           -> 解压覆盖（config 自动备份）
#    3. 解压来的目录、没有 .git     -> 加 -AdoptGit 转成 git 仓库
#
#  常用：
#    .\deploy\update.ps1                         # 更新 + 重启
#    .\deploy\update.ps1 -NoRestart              # 只更新代码，不动进程
#    .\deploy\update.ps1 -Zip C:\pandabot-deploy.zip
#    .\deploy\update.ps1 -AdoptGit               # 一次性：把解压目录接进 git
#
#  为什么 pull 完必须重启：
#    Python 启动时把 .py 编译成字节码加载进内存了。只换磁盘上的文件，
#    正在跑的进程一行代码都不会变 —— 表现就是「明明改了却没生效」。
#    而且 run-bot.cmd 是个死循环，只杀 python 没用：父进程 cmd 等 10 秒
#    会把旧代码再拉起来。所以必须「先停计划任务（杀 cmd）再杀 python」。
#
#  注意：本文件必须保存为 UTF-8 with BOM。
#        PowerShell 5.1 读无 BOM 文件时按 GBK 解码，中文注释的字节会被
#        当成双字节字符吃掉行尾换行，导致「语法错误但看不出哪错了」。
# ============================================================

param(
    [string]$Target = "C:\pandabot",
    [string]$Repo   = "https://github.com/qgz23/pandabot.git",
    [string]$Branch = "main",
    [string]$Zip    = "",
    [switch]$AdoptGit,
    [switch]$NoRestart,
    [int]$ProbeSeconds = 15
)

$ErrorActionPreference = "Stop"
# Expand-Archive 在 PS 5.1 会刷几百行进度条，把关键信息全冲没了
$ProgressPreference = "SilentlyContinue"
$TaskName = "PandaBot"

function Ok   { param([string]$m) Write-Host "[ OK ] $m" -ForegroundColor Green }
function Warn { param([string]$m) Write-Host "[注意] $m" -ForegroundColor Yellow }
function Bad  { param([string]$m) Write-Host "[错误] $m" -ForegroundColor Red }
function Die  { param([string]$m) Bad $m; Write-Host ""; exit 1 }
function Step { param([string]$m) Write-Host ""; Write-Host "==== $m ====" -ForegroundColor Cyan }

# ------------------------------------------------------------
#  原生命令（git / schtasks）统一从这里调。
#
#  为什么不能直接写 `& schtasks /query ... 2>&1`：
#    PowerShell 5.1 有个坑 —— 当 $ErrorActionPreference = "Stop" 时，
#    原生命令往 stderr 写任何内容都会被当成「终止性错误」抛出，
#    哪怕退出码是正常的探测失败（比如 git remote remove 一个不存在的
#    remote、schtasks 查一个不存在的任务）。表现就是脚本毫无征兆中断。
#    所以这里临时把 ErrorActionPreference 降成 Continue，只按退出码判断。
# ------------------------------------------------------------
function Invoke-Native {
    param([string]$Exe, [string[]]$ArgList = @())
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $raw = & $Exe @ArgList 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    $lines = @()
    foreach ($x in @($raw)) { if ($null -ne $x) { $lines += "$x" } }
    return @{ Code = $code; Out = ($lines -join "`n") }
}

# ------------------------------------------------------------
#  config 下三个 json 在版本控制里，覆盖会把服务器上的改动冲掉，先留底
# ------------------------------------------------------------
function Backup-Config {
    param([string]$RootDir)
    $cfgDir = Join-Path $RootDir "config"
    if (-not (Test-Path $cfgDir)) { return }
    $files = @(Get-ChildItem $cfgDir -Filter *.json -File -ErrorAction SilentlyContinue)
    if ($files.Count -eq 0) { return }
    $bak = Join-Path $cfgDir ("_backup_" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    New-Item -ItemType Directory -Path $bak -Force | Out-Null
    foreach ($f in $files) { Copy-Item $f.FullName -Destination $bak -Force }
    Ok "config 下 $($files.Count) 个 json 已备份到 config\$(Split-Path $bak -Leaf)"
}

# ------------------------------------------------------------
#  git 在部分国内网络下会卡在证书吊销检查（CRYPT_E_NO_REVOCATION_CHECK）。
#  这不是墙，是查吊销列表的服务器访问不了。遇到才降级，不预先关。
# ------------------------------------------------------------
function Invoke-GitSafe {
    param([string[]]$GitArgs)
    $r = Invoke-Native "git" $GitArgs
    if ($r.Code -eq 0) { return $r }
    if ($r.Out -match "CRYPT_E_NO_REVOCATION_CHECK|SSL certificate problem|schannel") {
        Warn "证书吊销检查失败（国内网络常见，不是墙），对本仓库关闭 sslVerify 后重试一次"
        Invoke-Native "git" @("config", "--local", "http.sslVerify", "false") | Out-Null
        return (Invoke-Native "git" $GitArgs)
    }
    return $r
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Write-Host ""
Write-Host "  PandaBot 更新 + 重启" -ForegroundColor White
Write-Host ""

# ============================================================
# 1. 定位项目根目录
# ============================================================
Step "1/5 定位项目目录"

if (-not (Test-Path $Target)) { Die "目录不存在：$Target" }

$root = $Target
if (-not (Test-Path (Join-Path $root "bot.py"))) {
    $subs = @(Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
              Where-Object { Test-Path (Join-Path $_.FullName "bot.py") })
    if ($subs.Count -eq 1) {
        $root = $subs[0].FullName
        Warn "代码在子目录里（压缩包多套了一层），已切到 $root"
    } else {
        Die "在 $Target 下找不到 bot.py，路径不对。用 -Target 指定项目根目录。"
    }
}
Ok "项目根：$root"

$hasGit = Test-Path (Join-Path $root ".git")
$gitCmd = Get-Command git -ErrorAction SilentlyContinue
$oldRev = ""

if ($hasGit) {
    $rv = Invoke-Native "git" @("-C", $root, "rev-parse", "--short", "HEAD")
    if ($rv.Code -eq 0) { $oldRev = $rv.Out.Trim() }
    Ok "当前版本：$oldRev"
    if (-not $gitCmd) { Die "这个目录是 git 仓库，但系统里找不到 git 命令。先装 Git，或用 -Zip 走离线包。" }
} else {
    Warn "这个目录没有 .git（是用压缩包解压出来的），无法 git pull"
}

# ============================================================
# 2. 更新代码
# ============================================================
Step "2/5 更新代码"

if ($Zip) {
    # ---------- 方式 A：离线包覆盖 ----------
    if (-not (Test-Path $Zip)) { Die "离线包不存在：$Zip" }
    Ok "使用离线包：$Zip"

    $tmp = Join-Path $env:TEMP ("pb-update-" + (Get-Date -Format "HHmmss"))
    Expand-Archive -Path $Zip -DestinationPath $tmp -Force

    # GitHub 的 zipball 会多套一层目录
    $src = $tmp
    if (-not (Test-Path (Join-Path $src "bot.py"))) {
        $sub = @(Get-ChildItem $src -Directory | Where-Object { Test-Path (Join-Path $_.FullName "bot.py") })
        if ($sub.Count -eq 1) { $src = $sub[0].FullName }
    }
    if (-not (Test-Path (Join-Path $src "bot.py"))) {
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        Die "离线包里找不到 bot.py，包是坏的或者下载不完整。"
    }

    Backup-Config $root
    Copy-Item (Join-Path $src "*") $root -Recurse -Force
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Ok "已覆盖到 $root"

} elseif ($hasGit) {
    # ---------- 方式 B：git pull ----------
    # 只看「被跟踪文件」的改动。未跟踪文件（?? 开头，比如 config/_backup_*）
    # 不影响 pull，不能因为它去 stash —— 那会白 stash 一次，还可能把
    # 用户以为已经保存的改动藏起来。
    $dirty = Invoke-Native "git" @("-C", $root, "status", "--porcelain")
    $trackedDirty = @()
    $untracked = 0
    if ($dirty.Code -eq 0) {
        foreach ($ln in ($dirty.Out -split "`n")) {
            if (-not $ln) { continue }
            if ($ln -match '^\?\?') { $untracked++ } else { $trackedDirty += $ln }
        }
    }
    if ($untracked -gt 0) { Ok "另有 $untracked 个未跟踪文件，不影响 pull" }

    if ($trackedDirty.Count -gt 0) {
        Warn "服务器上有未提交的改动，先 stash 掉才能 pull："
        $trackedDirty | ForEach-Object { Write-Host "        $_" -ForegroundColor DarkGray }
        $st = Invoke-Native "git" @("-C", $root, "stash", "push", "-m", "update.ps1 auto stash")
        if ($st.Code -ne 0) { Die "git stash 失败：$($st.Out)" }
        Ok "已 stash，要找回：git stash list / git stash pop"
    }

    $r = Invoke-GitSafe @("-C", $root, "pull", "--ff-only", "origin", $Branch)
    if ($r.Code -ne 0) {
        Write-Host $r.Out -ForegroundColor DarkGray
        if ($r.Out -match "CONNECT tunnel failed|response 502|Could not resolve|Failed to connect") {
            Bad "连不上 GitHub。如果本机设了 http_proxy/https_proxy 指向一个已经关掉的代理，先清掉环境变量再试。"
        }
        Die "git pull 失败。本地和历史分叉的话执行 git fetch origin 然后 git reset --hard origin/$Branch（会丢弃未提交改动）"
    }
    if ($r.Out -match "Already up to date") { Ok "已经是最新，没有新提交" } else { Ok "已拉取" }

} elseif ($AdoptGit) {
    # ---------- 方式 C：把解压目录接进 git ----------
    if (-not $gitCmd) { Die "没有 git 命令，装好再来，或改用 -Zip 离线包。" }
    Warn "把 $root 转成 git 仓库。.env.prod / data / .venv / logs 都在 .gitignore 里，不会被碰。"

    Backup-Config $root

    $init = Invoke-Native "git" @("-C", $root, "init", "-q")
    if ($init.Code -ne 0) { Die "git init 失败：$($init.Out)" }

    # 失败要能回滚：把刚建的空 .git 删掉，别留个半成品让下次误判成「已是仓库」
    $adoptOk = $false
    try {
        # remote 不存在时这里会报错，属正常，忽略即可
        Invoke-Native "git" @("-C", $root, "remote", "remove", "origin") | Out-Null

        $r = Invoke-GitSafe @("-C", $root, "remote", "add", "origin", $Repo)
        if ($r.Code -ne 0) { Die "设置远程仓库失败：$($r.Out)" }

        # 显式建 origin/* 跟踪分支，否则 origin/main 不存在，后面 set-upstream 会失败
        Invoke-Native "git" @("-C", $root, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*") | Out-Null

        Write-Host "  拉取中（私有仓库可能要你登录 GitHub；也可用 -Repo 传带 PAT 的地址）..." -ForegroundColor DarkGray
        $r = Invoke-GitSafe @("-C", $root, "fetch", "origin")
        if ($r.Code -ne 0) {
            Write-Host $r.Out -ForegroundColor DarkGray
            Die "git fetch 失败。常见原因：没登录 GitHub（用 -Repo https://<PAT>@github.com/qgz23/pandabot.git）、服务器连不上 GitHub。"
        }

        $r = Invoke-GitSafe @("-C", $root, "reset", "--hard", "origin/$Branch")
        if ($r.Code -ne 0) { Die "git reset 失败：$($r.Out)" }

        Invoke-Native "git" @("-C", $root, "branch", "-M", $Branch) | Out-Null
        Invoke-Native "git" @("-C", $root, "branch", "--set-upstream-to=origin/$Branch", $Branch) | Out-Null
        $adoptOk = $true
    } finally {
        if (-not $adoptOk) {
            Warn "接管失败，回滚刚创建的 .git（目录内容保持原样）"
            Remove-Item (Join-Path $root ".git") -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    $rv = Invoke-Native "git" @("-C", $root, "rev-parse", "--short", "HEAD")
    Ok "已接管为 git 仓库，版本 $($rv.Out.Trim())"

} else {
    # ---------- 什么都没给 ----------
    Bad "这个目录不是 git 仓库，脚本不知道从哪拿新代码。三选一："
    Write-Host ""
    Write-Host "  A) 转成 git 仓库，以后一条命令更新（推荐，需已装 Git）" -ForegroundColor Yellow
    Write-Host "       .\deploy\update.ps1 -AdoptGit" -ForegroundColor DarkGray
    Write-Host "  B) 用离线包覆盖" -ForegroundColor Yellow
    Write-Host "       .\deploy\update.ps1 -Zip C:\pandabot-deploy.zip" -ForegroundColor DarkGray
    Write-Host "  C) 就地重装（setup-server.ps1 也会 pull）" -ForegroundColor Yellow
    Write-Host "       .\deploy\setup-server.ps1" -ForegroundColor DarkGray
    Write-Host ""
    exit 1
}

# 更新后版本比对
if (Test-Path (Join-Path $root ".git")) {
    $rv = Invoke-Native "git" @("-C", $root, "rev-parse", "--short", "HEAD")
    if ($rv.Code -eq 0) {
        $newRev = $rv.Out.Trim()
        if ($oldRev -and $oldRev -eq $newRev) {
            Ok "代码版本未变：$newRev"
        } else {
            Ok "版本：$oldRev -> $newRev"
        }
    }
}

# ============================================================
# 3. 补依赖（只在 pyproject.toml 变过时才跑）
# ============================================================
Step "3/5 检查依赖"

$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Warn "没有 .venv，跳过。跑一次 .\deploy\setup-server.ps1 建环境。"
} else {
    $pyproject = Join-Path $root "pyproject.toml"
    $stamp = Join-Path $root ".venv\.pandabot-deps"
    $needDeps = $true
    if ((Test-Path $pyproject) -and (Test-Path $stamp)) {
        if ((Get-Item $pyproject).LastWriteTime -le (Get-Item $stamp).LastWriteTime) { $needDeps = $false }
    }

    if (-not $needDeps) {
        Ok "pyproject.toml 没变，跳过 pip"
    } else {
        Write-Host "  pyproject.toml 有变动，同步依赖（十几秒）..." -ForegroundColor DarkGray
        $deps = @(
            "nonebot2[httpx,websockets]>=2.5.0",
            "nonebot-adapter-qq>=1.7.3",
            "aiomysql>=0.3.2",
            "pillow>=10.0"
        )
        foreach ($d in $deps) {
            $r = Invoke-Native $venvPy @("-m", "pip", "install", "--quiet", "--disable-pip-version-check", $d)
            if ($r.Code -ne 0) { Write-Host $r.Out -ForegroundColor DarkGray; Die "装依赖失败：$d" }
        }
        $probe = Invoke-Native $venvPy @("-c", "import nonebot, PIL, aiomysql, httpx; print('deps-ok')")
        if ($probe.Out -notmatch "deps-ok") {
            Write-Host $probe.Out -ForegroundColor DarkGray
            Die "依赖装上了但 import 失败，多半是 Pillow 缺 VC++ 运行库"
        }
        Set-Content -Path $stamp -Value (Get-Date -Format "o") -Encoding ASCII
        Ok "依赖已同步"
    }
}

if ($NoRestart) {
    Warn "指定了 -NoRestart，代码已更新但进程还是旧的。生效需要重启："
    Write-Host "       schtasks /end /tn $TaskName" -ForegroundColor DarkGray
    Write-Host "       schtasks /run /tn $TaskName" -ForegroundColor DarkGray
    exit 0
}

# ============================================================
# 4. 重启
# ============================================================
Step "4/5 重启进程"

if (-not $isAdmin) { Die "重启需要管理员权限（计划任务以 SYSTEM 运行）。用管理员身份重开 PowerShell 再跑。" }

$logPath = Join-Path $root "logs\bot.log"
$offset = 0
if (Test-Path $logPath) { $offset = (Get-Item $logPath).Length }

$q = Invoke-Native "schtasks" @("/query", "/tn", $TaskName)
$taskExists = ($q.Code -eq 0)

if ($taskExists) {
    Invoke-Native "schtasks" @("/end", "/tn", $TaskName) | Out-Null
    Ok "已停止计划任务"
} else {
    Warn "没有 $TaskName 计划任务，只能手动启动，开机不会自启"
}

Start-Sleep -Seconds 2

# 关键顺序：先杀 cmd 循环，再杀 python。
# run-bot.cmd 是 goto loop 死循环，只杀 python 的话它 10 秒后会把旧代码拉起来。
$cmdProcs = @(Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" -ErrorAction SilentlyContinue |
              Where-Object { $_.CommandLine -and $_.CommandLine -like "*run-bot.cmd*" })
foreach ($cp in $cmdProcs) { Stop-Process -Id $cp.ProcessId -Force -ErrorAction SilentlyContinue }
if ($cmdProcs.Count -gt 0) { Ok "已终止 $($cmdProcs.Count) 个启动器进程（cmd 循环）" }

$pyProcs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -and $_.CommandLine -match "bot\.py" })
foreach ($pp in $pyProcs) { Stop-Process -Id $pp.ProcessId -Force -ErrorAction SilentlyContinue }
if ($pyProcs.Count -gt 0) { Ok "已终止 $($pyProcs.Count) 个残留 python 进程" }

Start-Sleep -Seconds 2

$leftover = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
              Where-Object { $_.CommandLine -and $_.CommandLine -match "bot\.py" })
if ($leftover.Count -gt 0) {
    Warn "还有 $($leftover.Count) 个 python 进程杀不掉（可能是别的用户会话启动的）"
}

if ($taskExists) {
    $r = Invoke-Native "schtasks" @("/run", "/tn", $TaskName)
    if ($r.Code -ne 0) { Die "启动计划任务失败（退出码 $($r.Code)）：$($r.Out)" }
    Ok "已启动计划任务"
} else {
    Write-Host "  直接后台启动一次..." -ForegroundColor DarkGray
    Start-Process -FilePath (Join-Path $root "deploy\run-bot.cmd") -WindowStyle Hidden
    Ok "已启动"
}

# ============================================================
# 5. 自检：只看本次启动写进日志的部分
# ============================================================
Step "5/5 等待 $ProbeSeconds 秒，检查启动日志"

Start-Sleep -Seconds $ProbeSeconds

if (-not (Test-Path $logPath)) { Die "日志文件不存在：$logPath，进程可能根本没起来。" }

$fs = $null
$sr = $null
$newLog = ""
try {
    $fs = [System.IO.File]::Open($logPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    if ($fs.Length -lt $offset) { $offset = 0 }
    $fs.Seek($offset, [System.IO.SeekOrigin]::Begin) | Out-Null
    $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8)
    $newLog = $sr.ReadToEnd()
} finally {
    if ($sr) { $sr.Close() }
    if ($fs) { $fs.Close() }
}

$loaded    = ([regex]::Matches($newLog, "Succeeded to load plugin")).Count
$connected = $newLog -match "connected"
$dbNote    = $newLog -match "MySQL"
$traceback = $newLog -match "Traceback \(most recent call last\)"

Write-Host ""
if ($loaded -gt 0) { Ok "加载插件：$loaded 个" } else { Bad "一个插件都没加载 —— 进程可能启动就崩了" }
if ($connected)    { Ok "已连上 QQ" } else { Bad "没有连上 QQ" }
if ($dbNote)       { Warn "日志里有 MySQL 记录，去群里发 /数据库状态 确认有没有降级到 SQLite" }
if ($traceback)    { Warn "日志里有 Traceback，贴出来看看" }

Ok "自检结束。完整日志：$logPath"

Write-Host ""
Write-Host "  现在去群里发 /帮助 验证新功能是否生效。" -ForegroundColor Yellow
Write-Host "  看实时日志：Get-Content $logPath -Tail 50 -Wait -Encoding UTF8" -ForegroundColor DarkGray
Write-Host ""
