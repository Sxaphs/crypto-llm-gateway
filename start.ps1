# crypto-llm-mvp 启动脚本（PowerShell）
# 用法：
#   .\start.ps1          # 全量：严格校验 + 启动全部 7 个服务
#   .\start.ps1 -Base    # 底座：只启动 postgres redis new-api epusdt gateway，允许支付/管理密码待填
param(
    [switch]$Base
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$script:FAIL = 0

function Ok([string]$m)   { Write-Host "[OK]   $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "[WARN] $m" -ForegroundColor Yellow }
function Err([string]$m)  { Write-Host "[FAIL] $m" -ForegroundColor Red; $script:FAIL++ }

# --- 1. 校验 docker / docker compose -------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "[FAIL] 未找到 docker，请先安装 Docker" -ForegroundColor Red
    exit 1
}
try {
    docker compose version | Out-Null
    Ok "docker compose 可用"
} catch {
    Err "未找到 docker compose（需 Compose v2 插件；configs.content 另需 >= v2.23.1）"
    exit 1
}
docker info | Out-Null
if ($LASTEXITCODE -ne 0) {
    Err "Docker 守护进程未运行或当前用户无权限"
    exit 1
}

# --- 2. .env --------------------------------------------------------------
if (-not (Test-Path -LiteralPath '.env')) {
    if (Test-Path -LiteralPath '.env.example') {
        Copy-Item -LiteralPath '.env.example' -Destination '.env'
        Err "已从 .env.example 生成 .env，请先填好所有 CHANGE_ME 项后重新运行"
    } else {
        Err "缺少 .env 且没有 .env.example"
    }
    exit 1
}
Ok ".env 存在"

# --- 3. 读取 .env（字面读取）---------------------------------------------
function Get-EnvValue([string]$Key) {
    $lines = Get-Content -LiteralPath '.env'
    foreach ($line in $lines) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#')) { continue }
        $idx = $t.IndexOf('=')
        if ($idx -lt 0) { continue }
        $k = $t.Substring(0, $idx).Trim()
        $v = $t.Substring($idx + 1).Trim()
        if ($v.Length -ge 2 -and (($v[0] -eq '"' -and $v[$v.Length-1] -eq '"') -or ($v[0] -eq "'" -and $v[$v.Length-1] -eq "'"))) {
            $v = $v.Substring(1, $v.Length - 2)
        }
        if ($k -eq $Key) { return $v }
    }
    return $null
}

function Test-BlankOrChangeMe([string]$v) {
    if ([string]::IsNullOrWhiteSpace($v)) { return $true }
    $vl = $v.ToLower()
    return ($vl -eq 'change_me' -or $vl -eq 'changeme')
}
function Test-IsLocal([string]$h) {
    return ($h -match '(^|\.)localhost$' -or $h -match '^\d+\.\d+\.\d+\.\d+$')
}
$WeakPasswords = @('password','passwd','123456','12345678','123456789','1234567890','admin','root','secret','postgres','redis','letmein','qwerty')

# --- 4. 校验辅助 ----------------------------------------------------------
function Test-Required([string]$Name, [int]$MinLen = 0, [switch]$CheckWeak) {
    $v = Get-EnvValue $Name
    if (Test-BlankOrChangeMe $v) { Err "$Name 仍为 CHANGE_ME/空，请填入真实值"; return }
    if ($MinLen -gt 0 -and $v.Length -lt $MinLen) { Err "$Name 长度不足 $MinLen 位（当前 $($v.Length) 位）"; return }
    if ($CheckWeak -and ($WeakPasswords -contains $v.ToLower())) { Err "$Name 使用了弱口令/默认值，请更换"; return }
    Ok "$Name 已配置"
}
function Test-SoftWarn([string]$Name, [string]$Desc) {
    $v = Get-EnvValue $Name
    if (Test-BlankOrChangeMe $v) { Warn "$Name 仍为 CHANGE_ME/空（$Desc）" }
}

# --- 5. 核心密钥（两种模式都必填）-----------------------------------------
Test-Required 'POSTGRES_PASSWORD' 12 -CheckWeak
Test-Required 'REDIS_PASSWORD' 12 -CheckWeak
Test-Required 'SESSION_SECRET' 32 -CheckWeak
Test-Required 'EPAY_HMAC_KEY' 16

$HASH = Get-EnvValue 'ADMIN_BASIC_AUTH_HASH'
if (Test-BlankOrChangeMe $HASH) {
    Err "ADMIN_BASIC_AUTH_HASH 仍为 CHANGE_ME，请用: docker run --rm caddy:2-alpine caddy hash-password --plaintext '强密码' 生成"
} elseif ($HASH -notmatch '^\$2[aby]\$') {
    Err "ADMIN_BASIC_AUTH_HASH 不是合法的 bcrypt 哈希（应以 `$2a`$ / `$2b`$ / `$2y`$ 开头）"
} else {
    Ok "ADMIN_BASIC_AUTH_HASH 已配置"
}

# 域名（四个）
foreach ($d in @('SITE_DOMAIN','PAY_DOMAIN','NEW_API_ADMIN_DOMAIN','EPUSDT_ADMIN_DOMAIN')) {
    $v = Get-EnvValue $d
    if (Test-BlankOrChangeMe $v) { Err "$d 仍为 CHANGE_ME/空" } else { Ok "$d = $v" }
}

# ACME：任一域名非 localhost 即需要有效邮箱
$NEEDS_ACME = $false
foreach ($d in @('SITE_DOMAIN','PAY_DOMAIN','NEW_API_ADMIN_DOMAIN','EPUSDT_ADMIN_DOMAIN')) {
    $v = Get-EnvValue $d
    if (-not (Test-IsLocal $v)) { $NEEDS_ACME = $true }
}
$ACME = Get-EnvValue 'ACME_EMAIL'
if ($NEEDS_ACME) {
    if (Test-BlankOrChangeMe $ACME) { Err "生产公网域名需要填写 ACME_EMAIL（用于 Let's Encrypt/ZeroSSL 证书）" }
    else { Ok "ACME_EMAIL 已配置" }
} else {
    if (Test-BlankOrChangeMe $ACME) { Write-Host "本地测试模式：ACME_EMAIL 可留空（Caddy 使用内部 CA 签发 localhost 证书）" }
    else { Ok "ACME_EMAIL 已配置" }
}

# --- 6. 支付/管理密钥：base 模式允许待填，全量模式严格校验 ----------------
if ($Base) {
    Write-Host "base 模式：支付/管理密钥允许待填（稍后回填再全量启动）"
    Test-SoftWarn 'EPAY_MD5_KEY' '支付密钥（base 模式可后填）'
    Test-SoftWarn 'GMPAY_PID' 'GMPay 商户号（后台创建后回填）'
    Test-SoftWarn 'GMPAY_SECRET_KEY' 'GMPay 密钥（后台创建后回填）'
    Test-SoftWarn 'EPUSDT_ADMIN_USERNAME' 'epusdt 管理员账号（默认 admin）'
    Test-SoftWarn 'EPUSDT_ADMIN_PASSWORD' 'epusdt 首启随机生成，改密/取密后回填'
} else {
    Test-Required 'EPAY_MD5_KEY' 16
    Test-Required 'GMPAY_PID' 4
    Test-Required 'GMPAY_SECRET_KEY' 16
    Test-Required 'EPUSDT_ADMIN_USERNAME' 1
    Test-Required 'EPUSDT_ADMIN_PASSWORD' 8
    Test-Required 'NEW_API_ADMIN_USERNAME' 1
    Test-Required 'NEW_API_ADMIN_PASSWORD' 8
    Test-Required 'TRONGRID_API_KEY' 8
    Test-Required 'ETHERSCAN_API_KEY' 8
    Test-Required 'TELEGRAM_BOT_TOKEN' 8
    Test-Required 'TELEGRAM_CHAT_ID' 1
}

# --- 7. base 模式的后续依赖提示 --------------------------------------------
if ($Base) {
    Test-SoftWarn 'NEW_API_ADMIN_USERNAME' 'New API 管理员账号（对账/脚本用）'
    Test-SoftWarn 'NEW_API_ADMIN_PASSWORD' 'New API 管理员密码（对账/脚本用）'
    Test-SoftWarn 'TRONGRID_API_KEY' '对账 TRC20 链上查账需要'
    Test-SoftWarn 'ETHERSCAN_API_KEY' '对账 ERC20 链上查账需要'
    Test-SoftWarn 'TELEGRAM_BOT_TOKEN' '对账告警需要'
    Test-SoftWarn 'TELEGRAM_CHAT_ID' '对账告警需要'
}

# --- 8. 汇总 --------------------------------------------------------------
if ($script:FAIL -gt 0) {
    Err "共 $script:FAIL 项未通过校验，请修正 .env 后重新运行（本脚本不启动）"
    exit 1
}

# --- 9. compose 配置校验 ----------------------------------------------------
Ok "开始校验 compose 配置（docker compose config -q）"
docker compose config -q
if ($LASTEXITCODE -ne 0) {
    Err "docker compose config 校验失败，请检查 docker-compose.yml 与 .env"
    exit 1
}
Ok "compose 配置校验通过"

# --- 10. 构建并启动 ----------------------------------------------------------
if ($Base) {
    Write-Host "base 模式：仅启动 postgres redis new-api epusdt gateway"
    docker compose up -d --build postgres redis new-api epusdt gateway
} else {
    Write-Host "全量启动：docker compose up -d --build"
    docker compose up -d --build
}
if ($LASTEXITCODE -ne 0) {
    Err "docker compose up 失败"
    exit 1
}

# --- 11. 使用提示 ------------------------------------------------------------
$SITE = Get-EnvValue 'SITE_DOMAIN'
$PAY  = Get-EnvValue 'PAY_DOMAIN'
$NAD  = Get-EnvValue 'NEW_API_ADMIN_DOMAIN'
$EAD  = Get-EnvValue 'EPUSDT_ADMIN_DOMAIN'

Write-Host ""
Write-Host "============================================================"
Write-Host "启动完成。访问入口："
Write-Host "  公网用户站        https://$SITE"
Write-Host "  公共收银台        https://$PAY"
Write-Host "  New API 管理域   https://$NAD   （管理页面 basic_auth，API 使用原生鉴权）"
Write-Host "  epusdt 管理域    https://$EAD    （管理页面 basic_auth，API 使用原生鉴权）"
Write-Host "  管理域 basic_auth 用户名固定为 admin"
Write-Host ""
Write-Host "常用命令："
Write-Host "  查看状态      docker compose ps"
Write-Host "  查看日志      docker compose logs -f <service>"
Write-Host "  停止          docker compose stop"

if ($Base) {
    Write-Host ""
    Write-Host "base 模式后续（在 .env 回填 GMPAY_PID / GMPAY_SECRET_KEY / EPUSDT_ADMIN_PASSWORD 后）："
    Write-Host "  全量启动：              .\start.ps1"
    Write-Host "  或仅补启支付/对账服务：  docker compose up -d --build payment-adapter reconciler"
    Write-Host ""
    Write-Host "另外："
    Write-Host "  1) epusdt 首启会在日志里打印随机管理员密码（用户名 admin）："
    Write-Host "       docker compose logs epusdt"
    Write-Host "  2) 登录 epusdt 管理域改密（或直接取该密码），回填 EPUSDT_ADMIN_PASSWORD。"
} else {
    Write-Host ""
    Write-Host "下一步："
    Write-Host "  1) 在 New API 管理域完成初始化向导并配置渠道。"
    Write-Host "  2) 在 epusdt 管理域配置钱包与 RPC 节点（收款地址/链上节点）。"
}
Write-Host "============================================================"
