# ============================================================
#  PandaBot 服务器一键部署（Windows Server）
#
#  在服务器上以「管理员 PowerShell」执行：
#      powershell -ExecutionPolicy Bypass -File .\setup-server.ps1
#
#  私有仓库要带令牌：
#      .\setup-server.ps1 -Token ghp_xxxxxxxx
#
#  全部参数：
#      -Target  C:\pandabot        部署目录（不能含中文和空格）
#      -Repo    <url>              仓库地址
#      -Token   <token>            GitHub 访问令牌（私有仓库必填）
#      -EnvFile D:\tmp\.env.prod   已有的 .env.prod，自动拷进项目
#      -PythonExe <path>          手动指定 python.exe（自动探测不中时用）
#      -NoTest                     跳过启动自检
#      -NoService                  不注册开机自启
#
#  这个脚本做 7 件事：
#      1 检查系统 / 时区 / Python / Git
#      2 拉代码（已存在就 pull）
#      3 建虚拟环境、装依赖
#      4 检查 .env.prod 和 data\waifu
#      5 启动 20 秒做自检，自动判断有没有连上 QQ
#      6 注册开机自启计划任务
#      7 打印后续操作清单
#
#  为什么要自己写这个而不 pip install -e .：
#      pyproject.toml 缺 [build-system]，pip 装不上。所以依赖手动列，
#      而且不装 nb-cli / nonebot-plugin-docs / htmlkit —— bot.py 用不到。
# ============================================================

[CmdletBinding()]
param(
    [string]$Target = "C:\pandabot",
    [string]$Repo = "https://github.com/qgz23/pandabot.git",
    [string]$Token = "",
    [string]$EnvFile = "",
    [string]$PythonExe = "",
    [switch]$NoTest,
    [switch]$NoService
)

$ErrorActionPreference = "Stop"

# 中文输出必须先把控制台切到 UTF-8，否则 Windows PowerShell 5.1 的 GBK
# 代码页会把中文打成乱码（脚本本身带 BOM，读取没问题，输出才是问题所在）
try {
    chcp 65001 > $null
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

# 子进程 python 也按 UTF-8 输出，这样上面设的 UTF-8 解码才对得上。
# 不设的话 Windows 上 python 默认按 GBK 写 stdout，接过来全是乱码。
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$TotalSteps = 7

# ---- 状态记录 ----------------------------------------------------
# 每一步的结果同时写进这个 JSON 文件。作用有两个：
#   1. 部署中途崩了，能看到卡在哪一步
#   2. 控制台中文万一显示成乱码或方框，让用户把这个文件发出来照样能看
# 放在 TEMP 而不是部署目录：Step 1 时部署目录可能还不存在
$StatusFile = Join-Path $env:TEMP "pandabot-setup-status.json"
$script:CurrentStep = "启动"
$script:Records = New-Object System.Collections.ArrayList

function Save-Status {
    $data = [ordered]@{
        script   = "deploy\setup-server.ps1"
        time     = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
        host     = $env:COMPUTERNAME
        status   = $script:Overall
        steps    = $script:Records
    }
    try {
        $json = $data | ConvertTo-Json -Depth 6
        [System.IO.File]::WriteAllText($StatusFile, $json, (New-Object System.Text.UTF8Encoding($false)))
    } catch { }
}

function Record($state, $detail) {
    $null = $script:Records.Add([ordered]@{
        step   = $script:CurrentStep
        state  = $state
        detail = $detail
    })
    Save-Status
}

$script:Overall = "running"
Save-Status

function Step($n, $msg) {
    $script:CurrentStep = "$n. $msg"
    Write-Host ""
    Write-Host ("[{0}/{1}] {2}" -f $n, $TotalSteps, $msg) -ForegroundColor Cyan
}
function Ok($msg)   { Write-Host "      [OK]   $msg" -ForegroundColor Green;  Record "ok"   $msg }
function Warn($msg) { Write-Host "      [注意] $msg" -ForegroundColor Yellow; Record "warn" $msg }
function Bad($msg)  { Write-Host "      [错误] $msg" -ForegroundColor Red;    Record "fail" $msg }
function Die($msg) {
    Bad $msg
    $script:Overall = "failed"
    Save-Status
    Write-Host ""
    Write-Host "部署中断，修好上面这条再重跑本脚本即可（已完成的步骤会自动跳过）。" -ForegroundColor Red
    Write-Host "诊断文件（中文乱码时把它发出来就行）：$StatusFile" -ForegroundColor DarkGray
    exit 1
}

# ============================================================
# 0. 前置检查
# ============================================================
Write-Host ""
Write-Host "============================================================" -ForegroundColor White
Write-Host "  PandaBot 服务器部署" -ForegroundColor White
Write-Host "============================================================" -ForegroundColor White

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Write-Host ""
Write-Host "  部署目录 : $Target"
Write-Host "  仓库     : $Repo"
Write-Host "  管理员   : $(if ($isAdmin) { '是' } else { '否' })"
if (-not $isAdmin) {
    Warn "不是管理员。拉代码和装依赖没问题，但最后注册开机自启会失败。"
    Warn "建议：右键 PowerShell → 以管理员身份运行，再执行本脚本。"
}

# 路径不能含中文或空格：Python 的 venv、schedule task 的参数传递在这种路径下
# 出过太多问题，宁可在最开始就拒绝
if ($Target -match '[^\x00-\x7F]') { Die "部署目录含中文：$Target。请换成纯英文路径，例如 C:\pandabot" }
if ($Target -match '\s') { Die "部署目录含空格：$Target。请换成 C:\pandabot 这类路径" }

Step 1 "检查运行环境"

# ---- 系统 ----
$os = Get-CimInstance Win32_OperatingSystem
Ok "系统 : $($os.Caption) (build $($os.BuildNumber))"
Ok "PowerShell : $($PSVersionTable.PSVersion)"

# ---- 时区 ----
# 不设的后果：服务器默认 UTC，比北京时间慢 8 小时。
# 签到按 date.today() 算日期，每日抽签种子也依赖本地日期 ——
# 会导致中国时间早 8 点前签到被算成前一天，抽老婆的结果提前 8 小时换新。
$tz = $null
try { $tz = (Get-TimeZone).Id } catch { }
$desired = "China Standard Time"
if ($tz -eq $desired) {
    Ok "时区 : 已是 $desired"
} else {
    try {
        Set-TimeZone -Id $desired -ErrorAction Stop
        Ok "时区 : 已从 $tz 改为 $desired"
    } catch {
        Warn "时区设置失败（当前 $tz）。请手动执行：Set-TimeZone -Id `"$desired`""
        Warn "不设的后果：签到日期、每日抽签会在早上 8 点提前换新"
    }
}

# ---- Python ----
function Get-PyVersion($exe) {
    $ver = & $exe -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $ver) { return $null }
    return ("$ver").Trim()
}

function Test-PyUsable($exe) {
    # 返回 $null 表示可用，否则返回不可用的原因
    $ver = Get-PyVersion $exe
    if (-not $ver) { return "执行失败（可能是商店占位程序或已损坏）" }
    $parts = $ver -split '\.'
    if ([int]$parts[0] -ne 3) { return "是 Python $ver，需要 Python 3" }
    if ([int]$parts[1] -lt 10) { return "是 Python $ver，太旧（需要 3.10+）" }
    return $null
}

function Find-Python {
    # 显式指定优先
    if ($PythonExe) {
        if (-not (Test-Path $PythonExe)) {
            Write-Host "        -PythonExe 指向的文件不存在：$PythonExe" -ForegroundColor DarkGray
            return $null
        }
        $why = Test-PyUsable $PythonExe
        if ($why) {
            Write-Host "        -PythonExe $PythonExe 不可用：$why" -ForegroundColor DarkGray
            return $null
        }
        return @{ Exe = $PythonExe; Version = (Get-PyVersion $PythonExe) }
    }

    # 自动探测。为什么要跳过 WindowsApps：
    #   那里的 python.exe 是个 0 字节占位程序，双击或执行只会弹出微软商店，
    #   不报错也不返回版本，看起来像「装了但用不了」。
    #   服务器（Windows Server）没有微软商店，从这里装的 Python 基本都是
    #   python.org 的安装包，路径在 Program Files 或 %LOCALAPPDATA%\Programs。
    $seen = New-Object System.Collections.ArrayList

    foreach ($cand in @("python", "python3", "py")) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if (-not $cmd) {
            $null = $seen.Add("$cand : 命令不在 PATH 里")
            continue
        }
        if ($cmd.Source -like "*\WindowsApps\*") {
            $null = $seen.Add("$cand : 指向微软商店占位程序，跳过")
            continue
        }
        $why = Test-PyUsable $cmd.Source
        if ($why) {
            $null = $seen.Add("$cand : $why")
            continue
        }
        return @{ Exe = $cmd.Source; Version = (Get-PyVersion $cmd.Source) }
    }

    Write-Host "        探测过程：" -ForegroundColor DarkGray
    foreach ($s in $seen) { Write-Host "          $s" -ForegroundColor DarkGray }
    return $null
}

$py = Find-Python
if (-not $py) {
    Bad "没找到 Python 3.10 或更高版本。"
    Write-Host ""
    Write-Host "  装 Python 的办法（推荐 3.13）：" -ForegroundColor Yellow
    Write-Host "    1. 打开 https://www.python.org/downloads/windows/" -ForegroundColor Yellow
    Write-Host "    2. 下载 Windows installer (64-bit) 的 .exe" -ForegroundColor Yellow
    Write-Host "    3. 双击安装，务必勾上「Add python.exe to PATH」" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  或者用一条命令静默装（要能联网）：" -ForegroundColor Yellow
    Write-Host '    $u = "https://www.python.org/ftp/python/3.13.7/python-3.13.7-amd64.exe"' -ForegroundColor DarkGray
    Write-Host '    Invoke-WebRequest -UseBasicParsing $u -OutFile "$env:TEMP\py.exe"' -ForegroundColor DarkGray
    Write-Host '    & "$env:TEMP\py.exe" /quiet InstallAllUsers=1 PrependPath=1' -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  装完关掉 PowerShell 重开（PATH 要重新读），再跑一次本脚本。" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  已经装了、但还是报这个错？直接告诉脚本 Python 在哪：" -ForegroundColor Yellow
    Write-Host '    .\setup-server.ps1 -PythonExe "C:\Program Files\Python313\python.exe"' -ForegroundColor DarkGray
    Write-Host '  不知道路径的话执行  where.exe python  看一眼' -ForegroundColor DarkGray
    exit 1
}
Ok "Python : $($py.Version)  $($py.Exe)"

# ---- Git（可选）----
# Git 只在「从远端 clone / pull」时才用到。离线包解压部署走的是手动上传模式，
# 全程不需要 Git —— 所以这里不能硬性 exit，否则离线部署会被第一道门直接挡死。
$git = Get-Command git -ErrorAction SilentlyContinue
if ($git) {
    Ok "Git   : $((& git --version))"
} else {
    Warn "没找到 Git。代码已经手动放好的话不影响部署；只在需要自动 clone/pull 时才装。"
    Write-Host "  装法（可选）：winget install --id Git.Git -e    或 https://git-scm.com/download/win" -ForegroundColor DarkGray
}

# ============================================================
# 2. 拉代码
# ============================================================
Step 2 "拉取代码到 $Target"

if (Test-Path $Target) {
    # 常见坑：资源管理器右键「全部解压缩」会多套一层同名目录，
    # 结果 bot.py 落在 $Target\pandabot-deploy\bot.py 而不是 $Target\bot.py。
    # 唯一子目录能确定时自动下钻一层，省得用户手动搬文件。
    if (-not (Test-Path (Join-Path $Target "bot.py")) -and -not (Test-Path (Join-Path $Target ".git"))) {
        $subs = @(Get-ChildItem -Path $Target -Directory -ErrorAction SilentlyContinue |
                  Where-Object { Test-Path (Join-Path $_.FullName "bot.py") })
        if ($subs.Count -eq 1) {
            $Target = $subs[0].FullName
            Ok "检测到代码在子目录里（压缩包多套了一层），已自动切到 $Target"
        }
    }
    $hasGit = Test-Path (Join-Path $Target ".git")
    $hasBot = Test-Path (Join-Path $Target "bot.py")

    if ($hasGit) {
        if (-not $git) {
            Die "目录 $Target 是 git 仓库，但系统里没有 git 命令，无法 pull 更新。请先装 Git（winget install --id Git.Git -e），或把该目录删掉改用手动上传模式。"
        }
        Ok "目录已存在且是 git 仓库，执行 pull"
        Push-Location $Target
        try {
            # 有未提交改动时 pull 会冲突，先看状态
            $dirty = (& git status --porcelain)
            if ($dirty) {
                Warn "服务器上有本地改动，先 stash 掉以便 update："
                $dirty -split "`n" | ForEach-Object { Write-Host "        $_" -ForegroundColor DarkGray }
                & git stash push -m "setup-server auto stash" | Out-Null
                Ok "已 stash（要找回：git stash list / git stash pop）"
            }
            & git pull --ff-only
            if ($LASTEXITCODE -ne 0) {
                Pop-Location
                Die "git pull 失败。若提示本地有分叉，手动执行 git fetch origin && git reset --hard origin/main"
            }
            Ok "已更新到最新：$((& git log --oneline -1))"
        } finally {
            Pop-Location
        }
    } elseif ($hasBot) {
        # 手动上传模式：用户把整个项目文件夹拷过来（或者解压了离线包）。
        # 这种目录没有 .git，不能 pull，但代码是全的，直接把剩下几步跑完就行 ——
        # 不能因为「不是 git 仓库」就拒绝安装，那会让离线部署走不通。
        Ok "目录已存在且有 bot.py，按「手动上传模式」继续（没有 .git，不做 pull）"
        Warn "以后更新代码要重新上传覆盖，脚本不会自动拉取"
    } else {
        Die "目录 $Target 已存在，但既不是 git 仓库、也没有 bot.py。为安全起见本脚本不删目录，请你确认后手动处理。"
    }
} else {
    # 目录还不存在 —— 只能靠 git clone。没 Git 就直接把离线包的路指清楚，
    # 别丢一句「没找到 Git」让用户在服务器上抓瞎。
    if (-not $git) {
        Bad "目录 $Target 不存在，且系统没有 Git，没法自动克隆代码。"
        Write-Host ""
        Write-Host "  改走离线包（不用装 Git，5 分钟搞定）：" -ForegroundColor Yellow
        Write-Host "    ① 把本机的 pandabot-deploy.zip 拷到服务器" -ForegroundColor Yellow
        Write-Host "    ② 解压到 $Target（解压后必须有 $Target\bot.py）" -ForegroundColor Yellow
        Write-Host "    ③ 重新运行本脚本，它会自动识别成「手动上传模式」继续" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  解压命令：" -ForegroundColor Cyan
        Write-Host "    Expand-Archive -Path <zip路径> -DestinationPath $Target -Force" -ForegroundColor DarkGray
        Write-Host ""
        Write-Host "  硬要装 Git 也行：winget install --id Git.Git -e  （装完关掉 PowerShell 重开）" -ForegroundColor DarkGray
        exit 1
    }
    $parent = Split-Path -Parent $Target
    if ($parent -and -not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }

    if ($Token) {
        $authUrl = $Repo -replace '^https://', "https://$Token@"
        Ok "带令牌 clone（令牌只在本条命令里用，不会留在 .git\config）"
    } else {
        $authUrl = $Repo
        if ($Repo -match 'github\.com/qgz23') {
            Warn "没给 -Token。私有仓库直接 clone 会要账号密码，建议改用："
            Warn "    .\setup-server.ps1 -Token <你的 GitHub 令牌>"
        }
    }

    & git clone $authUrl $Target
    if ($LASTEXITCODE -ne 0) { Die "git clone 失败，检查网络和令牌（令牌需要 repo 权限）" }

    # 立刻把 remote 改回不含令牌的地址，否则令牌会明文躺在 .git\config 里
    Push-Location $Target
    & git remote set-url origin $Repo
    Pop-Location
    Ok "clone 完成，remote 已清理成 $Repo"
}

$root = $Target
$venvPath = Join-Path $root ".venv"
$venvPy = Join-Path $venvPath "Scripts\python.exe"

# ============================================================
# 3. 虚拟环境与依赖
# ============================================================
Step 3 "创建虚拟环境并安装依赖"

$venvUsable = $false
if (Test-Path $venvPy) {
    # 光看文件在不在不够。虚拟环境里写死了创建时的解释器路径，
    # 从别的机器（或别的目录）拷过来的 .venv 在服务器上通常是坏的 ——
    # 表现是执行 python.exe 直接报「不是此操作系统平台的有效应用程序」。
    #
    # 注意：这种错误是 ApplicationFailedException（终止性错误），
    # 脚本全局 ErrorActionPreference = "Stop" 会直接终止整个脚本。
    # 所以这里必须临时切成 Continue 再 try 兜住 —— 否则「检查一下 venv 好不好」
    # 自己把部署搞挂了。
    Write-Host "      检查现有 .venv 是否可用..." -ForegroundColor DarkGray
    $probe = $null
    $oldEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $probe = & $venvPy -c "print('venv-ok')" 2>$null
    } catch {
        $probe = $null
    } finally {
        $ErrorActionPreference = $oldEAP
    }

    if ("$probe" -match "venv-ok") {
        $venvUsable = $true
        Ok "虚拟环境已存在且可用：$venvPath"
    } else {
        Warn "现有 .venv 跑不起来（多是从别的机器拷过来的），删掉重建"
        $ErrorActionPreference = "Continue"
        try { Remove-Item $venvPath -Recurse -Force } catch { }
        $ErrorActionPreference = $oldEAP
    }
}

if (-not $venvUsable) {
    & $py.Exe -m venv $venvPath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) { Die "创建虚拟环境失败：$venvPath" }
    Ok "已创建 $venvPath"
}

& $venvPy -m pip install --upgrade pip --quiet
Ok "pip 已升级"

# 只装代码真正 import 的东西。nonebot-plugin-docs / htmlkit 是 nb-cli 用的，
# bot.py 不加载它们，少装两个能少一半体积。
$deps = @(
    "nonebot2[httpx,websockets]>=2.5.0",
    "nonebot-adapter-qq>=1.7.3",
    "aiomysql>=0.3.2",
    "pillow>=10.0"
)
foreach ($d in $deps) {
    & $venvPy -m pip install --quiet $d
    if ($LASTEXITCODE -ne 0) { Die "装依赖失败：$d" }
    Ok "已装 $d"
}

# 确认能 import：光看 pip 成功不够，Pillow 在某些环境下装成功但缺 DLL
$probe = & $venvPy -c "import nonebot, PIL, aiomysql, httpx; print('deps-ok')" 2>&1
if ("$probe" -notmatch "deps-ok") {
    Bad "依赖装上了但 import 失败："
    Write-Host "$probe" -ForegroundColor DarkGray
    Die "通常是 Pillow 缺 VC++ 运行库，装一下 Microsoft Visual C++ Redistributable 2015-2022 x64"
}
Ok "import 自检通过"

# ============================================================
# 4. 配置文件与数据
# ============================================================
Step 4 "检查配置文件和运行时数据"

$envPath = Join-Path $root ".env.prod"
if ($EnvFile) {
    if (-not (Test-Path $EnvFile)) { Die "-EnvFile 指向的文件不存在：$EnvFile" }
    Copy-Item $EnvFile $envPath -Force
    Ok "已从 $EnvFile 拷入 .env.prod"
}

if (Test-Path $envPath) {
    Ok "配置文件存在：$envPath"
    # 顺手检查两个最容易配错的项，只提示不改
    $envText = Get-Content $envPath -Raw -Encoding UTF8
    if ($envText -match 'QQ_IS_SANDBOX\s*=\s*"?true"?') {
        Warn "当前是沙箱环境（QQ_IS_SANDBOX=true），只能操作开放平台里配置的沙箱群"
        Warn "要进真实群：改成 false，并先把服务器公网 IP 加进开放平台的 IP 白名单"
    }
    if ($envText -notmatch 'DB_HOST') {
        Warn ".env.prod 里没有 DB_HOST，会降级用本地 SQLite，搭档那边看不到数据"
    }

    # ---- 云数据库连通性 ----
    # store.py 连不上 MySQL 时会静默降级成本地 SQLite —— 机器人照样跑、照样签到，
    # 数据只是悄悄写进服务器自己的文件。等发现时两边已经对不上了，所以现在就测。
    $dbCheck = Join-Path $root "deploy\check-db.py"
    if (Test-Path $dbCheck) {
        Write-Host "      正在测试云数据库（最多 20 秒）..." -ForegroundColor DarkGray
        $dbRaw = & $venvPy $dbCheck 2>&1
        $dbText = (($dbRaw | Out-String) -replace "`r", "")
        $dbLines = @($dbText -split "`n" | Where-Object { $_.Trim() -ne "" })

        if ($dbText -match "\[DB-OK\]") {
            Ok "云数据库连上了"
            foreach ($ln in $dbLines) {
                if ($ln -match "\[DB-OK\]") { continue }
                Write-Host "        $($ln.Trim())" -ForegroundColor DarkGray
            }
        } elseif ($dbText -match "\[DB-SKIP\]") {
            Warn "没配置云数据库 —— 将使用本地 SQLite（单机测试没问题）"
        } else {
            Bad "云数据库连不上！机器人会降级成本地 SQLite，你搭档那边看不到数据"
            foreach ($ln in $dbLines) { Write-Host "        $($ln.Trim())" -ForegroundColor DarkGray }
            Write-Host "      这里不中断部署（本地 SQLite 也能跑），但上线前务必修好。" -ForegroundColor Yellow
        }
    } else {
        Warn "找不到 deploy\check-db.py，跳过数据库检查"
    }
} else {
    Bad "缺少 .env.prod —— 这个文件被 .gitignore 排除，git 拉不到，必须手动放。"
    Write-Host ""
    Write-Host "  两种办法：" -ForegroundColor Yellow
    Write-Host "    A. 重新执行并指定文件位置：" -ForegroundColor Yellow
    Write-Host "         .\setup-server.ps1 -EnvFile D:\tmp\.env.prod" -ForegroundColor DarkGray
    Write-Host "    B. 直接把本地 pandabot\.env.prod 拷到 $envPath" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  里面是机器人凭证和数据库密码，不要传到公开的地方。" -ForegroundColor Yellow
    exit 1
}

# data\ 整个目录都被 gitignore 排除，所以角色图不会跟着 git 过来
$waifuDir = Join-Path $root "data\waifu"
if (Test-Path $waifuDir) {
    $imgs = @(Get-ChildItem (Join-Path $waifuDir "img") -Recurse -File -ErrorAction SilentlyContinue)
    Ok "角色图 $($imgs.Count) 张"
    if ($imgs.Count -eq 0) { Warn "data\waifu\img 是空的，/今日老婆 会走内置兜底池" }
} else {
    Warn "没有 data\waifu，/今日老婆 会走内置兜底角色池"
    Warn "要从本地搬的话，在本地执行（换成你的用户名和服务器 IP）："
    Warn "    scp -r data/waifu 用户名@服务器IP:C:/pandabot/data/"
}

# 日志目录
$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

# ============================================================
# 5. 启动自检
# ============================================================
Step 5 "启动自检（约 20 秒）"

if ($NoTest) {
    Warn "已用 -NoTest 跳过"
} else {
    # 如果之前注册过服务并在跑，先停掉，否则两个实例抢同一个机器人连接。
    # 整段必须容错：schtasks 在受限环境（安全策略、非管理员、被安全软件拦）
    # 会直接失败。那种情况下当作「没有计划任务」继续走，不能因此中断自检 ——
    # 自检失败和探测失败是两件事，不该混在一起。
    $taskExists = $false
    $oldEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $q = schtasks /query /tn "PandaBot" 2>$null
        if ($LASTEXITCODE -eq 0 -and $q) { $taskExists = $true }
    } catch {
        $taskExists = $false
    } finally {
        $ErrorActionPreference = $oldEAP
    }

    if ($taskExists) {
        $ErrorActionPreference = "Continue"
        try {
            schtasks /end /tn "PandaBot" 2>$null | Out-Null
            Start-Sleep -Seconds 2
            Warn "检测到已有 PandaBot 计划任务，已临时停止以便自检"
        } catch {
            Warn "已有 PandaBot 计划任务但停不下来 —— 自检可能因两个实例同时连接而失败"
        } finally {
            $ErrorActionPreference = $oldEAP
        }
    }

    $tmpOut = Join-Path $env:TEMP "pandabot_probe_out.log"
    $tmpErr = Join-Path $env:TEMP "pandabot_probe_err.log"
    Remove-Item $tmpOut, $tmpErr -Force -ErrorAction SilentlyContinue

    Push-Location $root
    $proc = Start-Process -FilePath $venvPy -ArgumentList "bot.py" `
        -WorkingDirectory $root -PassThru -NoNewWindow `
        -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr
    Pop-Location

    Write-Host "      已启动进程 PID $($proc.Id)，等待 20 秒..." -ForegroundColor DarkGray
    Start-Sleep -Seconds 20

    if (-not $proc.HasExited) {
        try { Stop-Process -Id $proc.Id -Force -ErrorAction Stop } catch { }
        Start-Sleep -Seconds 1
    }

    $out = ""
    if (Test-Path $tmpOut) { $out += (Get-Content $tmpOut -Raw -ErrorAction SilentlyContinue) }
    if (Test-Path $tmpErr) { $out += (Get-Content $tmpErr -Raw -ErrorAction SilentlyContinue) }
    if (-not $out) { $out = "" }

    $loaded = ([regex]::Matches($out, "Succeeded to load plugin")).Count
    $connected = ($out -match "connected")
    $traceback = ($out -match "Traceback \(most recent call last\)")

    Write-Host ""
    Write-Host "      ---- 自检结果 ----" -ForegroundColor DarkGray
    Write-Host "      加载插件 : $loaded 个" -ForegroundColor DarkGray

    if ($traceback) {
        Bad "启动过程抛了异常，最后 25 行如下："
        Write-Host ""
        ($out -split "`n" | Select-Object -Last 25) | ForEach-Object { Write-Host "        $_" -ForegroundColor DarkGray }
        Write-Host ""
        Die "先修掉上面这个异常。"
    }

    if ($loaded -lt 10) {
        Bad "只加载了 $loaded 个插件（应该 14 个左右），日志最后 25 行："
        Write-Host ""
        ($out -split "`n" | Select-Object -Last 25) | ForEach-Object { Write-Host "        $_" -ForegroundColor DarkGray }
        exit 1
    }
    Ok "插件加载 $loaded 个"

    if ($connected) {
        Ok "已连上 QQ"
        $line = ($out -split "`n" | Where-Object { $_ -match "connected" } | Select-Object -First 1)
        if ($line) { Write-Host "        $($line.Trim())" -ForegroundColor DarkGray }
    } else {
        Warn "没看到 connected。常见原因："
        Warn "  1. .env.prod 里 QQ_BOTS 的 appid/secret 填错"
        Warn "  2. 已切正式环境但服务器公网 IP 没加进开放平台的白名单"
        Warn "  3. 服务器出站 443 被防火墙拦了"
        Write-Host ""
        Write-Host "      日志最后 25 行：" -ForegroundColor DarkGray
        ($out -split "`n" | Select-Object -Last 25) | ForEach-Object { Write-Host "        $_" -ForegroundColor DarkGray }
    }

    Remove-Item $tmpOut, $tmpErr -Force -ErrorAction SilentlyContinue
}

# ============================================================
# 6. 注册开机自启
# ============================================================
Step 6 "注册开机自启"

if ($NoService) {
    Warn "已用 -NoService 跳过。手动启动：deploy\run-bot.cmd"
} elseif (-not $isAdmin) {
    Warn "不是管理员，跳过。换管理员 PowerShell 再执行一次，或者："
    Warn "    cd $root\deploy; powershell -ExecutionPolicy Bypass -File .\install-service.ps1"
} else {
    $installer = Join-Path $root "deploy\install-service.ps1"
    if (-not (Test-Path $installer)) { Die "找不到 $installer" }
    Push-Location (Join-Path $root "deploy")
    & powershell -NoProfile -ExecutionPolicy Bypass -File $installer
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { Die "注册计划任务失败（退出码 $code）" }
    Ok "开机自启已注册，任务名 PandaBot"
}

# ============================================================
# 7. 收尾
# ============================================================
Step 7 "完成"

$script:Overall = "done"
Record "done" "部署流程结束"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  部署完成" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  项目目录 : $root"
Write-Host "  虚拟环境 : $venvPath"
Write-Host "  日志     : $root\logs\bot.log"
Write-Host "  诊断文件 : $StatusFile"
Write-Host ""
Write-Host "  看实时日志：" -ForegroundColor Cyan
Write-Host "    Get-Content $root\logs\bot.log -Tail 50 -Wait" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  停 / 启 / 删 服务：" -ForegroundColor Cyan
Write-Host "    schtasks /end    /tn PandaBot" -ForegroundColor DarkGray
Write-Host "    schtasks /run    /tn PandaBot" -ForegroundColor DarkGray
Write-Host "    schtasks /delete /tn PandaBot /f" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  以后更新代码：" -ForegroundColor Cyan
if ($git -and (Test-Path (Join-Path $root ".git"))) {
    Write-Host "    cd $root; git pull; schtasks /end /tn PandaBot; schtasks /run /tn PandaBot" -ForegroundColor DarkGray
} else {
    Write-Host "    重新传一份代码覆盖目录，然后：" -ForegroundColor DarkGray
    Write-Host "    schtasks /end /tn PandaBot; schtasks /run /tn PandaBot" -ForegroundColor DarkGray
    Write-Host "    （这个目录不是 git 仓库，别指望 git pull）" -ForegroundColor DarkGray
}
Write-Host ""
Write-Host "  ★ 重要：部署完把本地那个 bot 停掉（Ctrl+C 或关掉 nb run 窗口）。" -ForegroundColor Yellow
Write-Host "    同一个机器人在两处同时连，群里会出现重复回复。" -ForegroundColor Yellow
Write-Host ""
Write-Host "  ★ 群里发 /帮助 和 /数据库状态 各试一次：" -ForegroundColor Yellow
Write-Host "    /帮助        → 能看到 14 个插件的命令，说明加载完整" -ForegroundColor Yellow
Write-Host "    /数据库状态  → 显示 MySQL 才是对的，显示 SQLite 说明没连上云库" -ForegroundColor Yellow
Write-Host ""
Write-Host "  想测敏感词过滤，先发 /过滤 看「其中 N 条没 @ 机器人」的 N 是否在增长：" -ForegroundColor Yellow
Write-Host "    N 一直是 0 → 全量消息权限没开（见下面的群设置）" -ForegroundColor Yellow
Write-Host ""
Write-Host "  群设置（只有群主能改，在手机 QQ 里）：" -ForegroundColor Yellow
Write-Host "    群设置 → 群机器人 → 熊猫娘" -ForegroundColor Yellow
Write-Host "      ① 机器人可获取的群聊消息范围 → 获取群内全部消息（敏感词过滤要用）" -ForegroundColor Yellow
Write-Host "      ② 机器人主动在群聊内发言 → 打开（定时播报要用）" -ForegroundColor Yellow
Write-Host "    群管理 → 设置管理员 → 勾上熊猫娘（禁言 / 撤回要用）" -ForegroundColor Yellow
Write-Host ""
