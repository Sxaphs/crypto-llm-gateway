#!/usr/bin/env bash
# crypto-llm-mvp 启动脚本
# 用法：
#   ./start.sh          # 全量：严格校验 + 启动全部 7 个服务
#   ./start.sh --base   # 底座：只启动 postgres redis new-api epusdt gateway，允许支付/管理密码待填
set -uo pipefail

cd "$(dirname "$0")" || exit 1

BASE_MODE=0
for a in "$@"; do
  case "$a" in
    --base|-b) BASE_MODE=1 ;;
    --help|-h) echo "用法: $0 [--base]"; exit 0 ;;
  esac
done

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'; NC=$'\033[0m'
FAIL=0

info()  { printf '%s\n' "$*"; }
ok()    { printf '%s[OK]%s %s\n' "$GREEN" "$NC" "$*"; }
warnf() { printf '%s[WARN]%s %s\n' "$YELLOW" "$NC" "$*"; }
errf()  { printf '%s[FAIL]%s %s\n' "$RED" "$NC" "$*"; FAIL=$((FAIL+1)); }

# --- 1. 校验 docker / docker compose -------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  errf "未找到 docker，请先安装 Docker"
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  errf "未找到 docker compose（需 Compose v2 插件；configs.content 另需 >= v2.23.1）"
  exit 1
fi
ok "docker compose 可用"
if ! docker info >/dev/null 2>&1; then
  errf "Docker 守护进程未运行或当前用户无权限"
  exit 1
fi

# --- 2. .env --------------------------------------------------------------
if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
    errf "已从 .env.example 生成 .env，请先填好所有 CHANGE_ME 项后重新运行"
  else
    errf "缺少 .env 且没有 .env.example"
  fi
  exit 1
fi
ok ".env 存在"

# --- 3. 字面读取 .env（不做 shell 展开）-----------------------------------
env_get() {
  local key="$1" line k v
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*) continue ;;
    esac
    k="${line%%=*}"
    v="${line#*=}"
    case "$v" in
      \"*\") v="${v#\"}"; v="${v%\"}" ;;
    esac
    case "$v" in
      \'*\') v="${v#\'}"; v="${v%\'}" ;;
    esac
    if [ "$k" = "$key" ]; then printf '%s\n' "$v"; return 0; fi
  done < .env
  return 1
}

# --- 4. 校验辅助 ----------------------------------------------------------
is_blank_or_change_me() {
  local v; v="$(printf '%s' "$1" | tr 'A-Z' 'a-z')"
  case "$v" in
    ''|change_me|changeme) return 0 ;;
    *) return 1 ;;
  esac
}
is_weak() {
  local v; v="$(printf '%s' "$1" | tr 'A-Z' 'a-z')"
  case "$v" in
    password|passwd|123456|12345678|123456789|1234567890|admin|root|secret|postgres|redis|letmein|qwerty) return 0 ;;
    *) return 1 ;;
  esac
}
is_local() {
  printf '%s' "$1" | grep -qE '(^|\.)localhost$|^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'
}
require() { # $1=变量名 $2=最小长度(0=不限) $3=是否检查弱口令(1/0)
  local name="$1" min="$2" chkweak="$3" v
  v="$(env_get "$name")" || { errf "缺少必需变量 $name"; return; }
  if is_blank_or_change_me "$v"; then
    errf "$name 仍为 CHANGE_ME/空，请填入真实值"
    return
  fi
  if [ "$min" -gt 0 ] && [ "${#v}" -lt "$min" ]; then
    errf "$name 长度不足 ${min} 位（当前 ${#v} 位）"
    return
  fi
  if [ "$chkweak" -eq 1 ] && is_weak "$v"; then
    errf "$name 使用了弱口令/默认值，请更换"
    return
  fi
  ok "$name 已配置"
}
soft_warn() { # $1=变量名 $2=说明
  local v; v="$(env_get "$1" || true)"
  if is_blank_or_change_me "$v"; then
    warnf "$1 仍为 CHANGE_ME/空（$2）"
  fi
}

# --- 5. 核心密钥（两种模式都必填）-----------------------------------------
require POSTGRES_PASSWORD 12 1
require REDIS_PASSWORD 12 1
require SESSION_SECRET 32 1
require EPAY_HMAC_KEY 16 0

HASH="$(env_get ADMIN_BASIC_AUTH_HASH || true)"
if is_blank_or_change_me "$HASH"; then
  errf "ADMIN_BASIC_AUTH_HASH 仍为 CHANGE_ME，请用: docker run --rm caddy:2-alpine caddy hash-password --plaintext '强密码' 生成"
elif ! printf '%s' "$HASH" | grep -qE '^\$2[aby]\$'; then
  errf "ADMIN_BASIC_AUTH_HASH 不是合法的 bcrypt 哈希（应以 \$2a\$/\$2b\$/\$2y\$ 开头）"
else
  ok "ADMIN_BASIC_AUTH_HASH 已配置"
fi

# 域名（四个）
for d in SITE_DOMAIN PAY_DOMAIN NEW_API_ADMIN_DOMAIN EPUSDT_ADMIN_DOMAIN; do
  v="$(env_get "$d" || true)"
  if is_blank_or_change_me "$v"; then
    errf "$d 仍为 CHANGE_ME/空"
  else
    ok "$d = $v"
  fi
done

# ACME：任一域名非 localhost 即需要有效邮箱
NEEDS_ACME=0
for d in SITE_DOMAIN PAY_DOMAIN NEW_API_ADMIN_DOMAIN EPUSDT_ADMIN_DOMAIN; do
  v="$(env_get "$d" || true)"
  if ! is_local "$v"; then NEEDS_ACME=1; fi
done
ACME="$(env_get ACME_EMAIL || true)"
if [ "$NEEDS_ACME" = "1" ]; then
  if is_blank_or_change_me "$ACME"; then
    errf "生产公网域名需要填写 ACME_EMAIL（用于 Let's Encrypt/ZeroSSL 证书）"
  else
    ok "ACME_EMAIL 已配置"
  fi
else
  if is_blank_or_change_me "$ACME"; then
    info "本地测试模式：ACME_EMAIL 可留空（Caddy 使用内部 CA 签发 localhost 证书）"
  else
    ok "ACME_EMAIL 已配置"
  fi
fi

# --- 6. 支付/管理密钥：base 模式允许待填，全量模式严格校验 ----------------
if [ "$BASE_MODE" = "1" ]; then
  info "base 模式：支付/管理密钥允许待填（稍后回填再全量启动）"
  soft_warn EPAY_MD5_KEY "支付密钥（base 模式可后填）"
  soft_warn GMPAY_PID "GMPay 商户号（后台创建后回填）"
  soft_warn GMPAY_SECRET_KEY "GMPay 密钥（后台创建后回填）"
  soft_warn EPUSDT_ADMIN_USERNAME "epusdt 管理员账号（默认 admin）"
  soft_warn EPUSDT_ADMIN_PASSWORD "epusdt 首启随机生成，改密/取密后回填"
else
  require EPAY_MD5_KEY 16 0
  require GMPAY_PID 4 0
  require GMPAY_SECRET_KEY 16 0
  require EPUSDT_ADMIN_USERNAME 1 0
  require EPUSDT_ADMIN_PASSWORD 8 0
  require NEW_API_ADMIN_USERNAME 1 0
  require NEW_API_ADMIN_PASSWORD 8 0
  require TRONGRID_API_KEY 8 0
  require ETHERSCAN_API_KEY 8 0
  require TELEGRAM_BOT_TOKEN 8 0
  require TELEGRAM_CHAT_ID 1 0
fi

# --- 7. base 模式的后续依赖提示 --------------------------------------------
if [ "$BASE_MODE" = "1" ]; then
  soft_warn NEW_API_ADMIN_USERNAME "New API 管理员账号（对账/脚本用）"
  soft_warn NEW_API_ADMIN_PASSWORD "New API 管理员密码（对账/脚本用）"
  soft_warn TRONGRID_API_KEY "对账 TRC20 链上查账需要"
  soft_warn ETHERSCAN_API_KEY "对账 ERC20 链上查账需要"
  soft_warn TELEGRAM_BOT_TOKEN "对账告警需要"
  soft_warn TELEGRAM_CHAT_ID "对账告警需要"
fi

# --- 8. 汇总 --------------------------------------------------------------
if [ "$FAIL" -gt 0 ]; then
  errf "共 $FAIL 项未通过校验，请修正 .env 后重新运行（本脚本不启动）"
  exit 1
fi

# --- 9. compose 配置校验 ---------------------------------------------------
ok "开始校验 compose 配置（docker compose config -q）"
if ! docker compose config -q; then
  errf "docker compose config 校验失败，请检查 docker-compose.yml 与 .env"
  exit 1
fi
ok "compose 配置校验通过"

# --- 10. 构建并启动 ---------------------------------------------------------
if [ "$BASE_MODE" = "1" ]; then
  info "base 模式：仅启动 postgres redis new-api epusdt gateway"
  if ! docker compose up -d --build postgres redis new-api epusdt gateway; then
    errf "底座服务构建或启动失败，请查看上方 Docker 错误"
    exit 1
  fi
else
  info "全量启动：docker compose up -d --build"
  if ! docker compose up -d --build; then
    errf "服务构建或启动失败，请查看上方 Docker 错误"
    exit 1
  fi
fi

# --- 11. 使用提示 -----------------------------------------------------------
cat <<EOF

============================================================
启动完成。访问入口：
  公网用户站        https://$(env_get SITE_DOMAIN)
  公共收银台        https://$(env_get PAY_DOMAIN)
  New API 管理域   https://$(env_get NEW_API_ADMIN_DOMAIN)   （管理页面 basic_auth，API 使用原生鉴权）
  epusdt 管理域    https://$(env_get EPUSDT_ADMIN_DOMAIN)    （管理页面 basic_auth，API 使用原生鉴权）
  管理域 basic_auth 用户名固定为 admin

常用命令：
  查看状态      docker compose ps
  查看日志      docker compose logs -f <service>
  停止          docker compose stop
EOF

if [ "$BASE_MODE" = "1" ]; then
  cat <<EOF

base 模式后续（在 .env 回填 GMPAY_PID / GMPAY_SECRET_KEY / EPUSDT_ADMIN_PASSWORD 后）：
  全量启动：              ./start.sh
  或仅补启支付/对账服务：  docker compose up -d --build payment-adapter reconciler

另外：
  1) epusdt 首启会在日志里打印随机管理员密码（用户名 admin）：
       docker compose logs epusdt | grep -A2 -i 'Default admin'
  2) 登录 epusdt 管理域改密（或直接取该密码），回填 EPUSDT_ADMIN_PASSWORD。
EOF
else
  cat <<EOF

下一步：
  1) 在 New API 管理域完成初始化向导并配置渠道。
  2) 在 epusdt 管理域配置钱包与 RPC 节点（收款地址/链上节点）。
EOF
fi

echo "============================================================"
