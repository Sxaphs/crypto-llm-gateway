"""环境变量读取与配置约定。

所有脚本共用同一套环境变量契约；本文件是唯一的事实来源。
自动读取项目根目录 .env（不覆盖已存在的环境变量；安全解析引号）。
密钥类变量绝不写进日志：读取后直接使用，不打印。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _parse_dotenv(path: Path) -> dict:
    """极简、安全的 .env 解析：KEY=VALUE / KEY="V" / KEY='V' / export KEY=V / # 注释。"""
    out: dict = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        # 去掉行内注释（仅在值未加引号时保守处理）
        if value and not (value[0] in ("'", '"')):
            value = value.split(" #", 1)[0].split("\t#", 1)[0].rstrip()
        out[key] = value
    return out


def _load_dotenv() -> None:
    for name, value in _parse_dotenv(PROJECT_ROOT / ".env").items():
        os.environ.setdefault(name, value)


_load_dotenv()


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def bool_env(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def int_env(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def float_env(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


@dataclass
class NewApiConfig:
    base_url: str = "http://localhost:3000"
    admin_username: str = "root"
    admin_password: str = ""  # 密钥，绝不打印
    basic_auth_username: str = ""  # 可选，经管理域(HTTP Basic)访问
    basic_auth_password: str = ""  # 密钥，绝不打印


@dataclass
class PaymentConfig:
    site_domain: str = "localhost"
    epay_id: str = ""  # New API EpayId（商户 pid）
    epay_key: str = ""  # New API EpayKey（MD5 key），密钥，绝不打印
    price_cny: float = 7.3
    min_topup: int = 1
    callback_address: str = ""
    notify_url: str = ""  # adapter -> New API 的回调地址


@dataclass
class SmtpConfig:
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""  # 密钥，绝不打印
    sender: str = ""
    ssl: bool = False
    force_auth_login: bool = False


@dataclass
class SiteConfig:
    system_name: str = "Crypto LLM"
    logo_text: str = "Crypto LLM"
    register_enabled: bool = True
    password_register_enabled: bool = True
    email_verification_enabled: bool = True


@dataclass
class ChannelConfig:
    name: str = ""
    type: int = 1
    base_url: str = ""
    key: str = ""  # 密钥，绝不打印
    models: list = field(default_factory=list)
    group: str = "default"
    model_mapping: str = ""


@dataclass
class ReconcileConfig:
    adapter_pg_dsn: str = ""  # adapter 的 DATABASE_URL
    new_api: NewApiConfig = field(default_factory=NewApiConfig)
    epusdt_base_url: str = ""
    epusdt_admin_username: str = "admin"
    epusdt_admin_password: str = ""  # 密钥，绝不打印
    trongrid_api_key: str = ""  # 密钥，绝不打印
    etherscan_api_key: str = ""  # 密钥，绝不打印
    usdc_polygon_contract: str = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
    quota_per_usd: int = 500000  # New API QuotaPerUnit == adapter QUOTA_PER_USD
    rate_max_age_seconds: int = 300  # adapter RATE_MAX_AGE_SECONDS
    wallet_addresses: list = field(default_factory=list)  # 用于"链上入账无订单"检测
    report_dir: str = "reports"
    tg_bot_token: str = ""  # 密钥，绝不打印
    tg_chat_id: str = ""


def load_new_api_config() -> NewApiConfig:
    return NewApiConfig(
        base_url=_get("NEW_API_BASE_URL", "http://localhost:3000").rstrip("/"),
        admin_username=_get("NEW_API_ADMIN_USERNAME", "root"),
        admin_password=_get("NEW_API_ADMIN_PASSWORD", ""),
        basic_auth_username=_get("ADMIN_BASIC_AUTH_USERNAME", ""),
        basic_auth_password=_get("ADMIN_BASIC_AUTH_PASSWORD", ""),
    )


def load_payment_config() -> PaymentConfig:
    domain = _get("SITE_DOMAIN", "localhost")
    # EPAY_ID 优先，其次 GMPAY_PID，再次 EPAY_PID（与 adapter epay_pid fallback 对齐）。
    epay_id = _get("EPAY_ID", "") or _get("GMPAY_PID", "") or _get("EPAY_PID", "")
    return PaymentConfig(
        site_domain=domain,
        epay_id=epay_id,
        epay_key=_get("EPAY_MD5_KEY", ""),
        price_cny=float_env("NEW_API_PRICE_CNY", 7.3),
        min_topup=int_env("NEW_API_MIN_TOPUP", 1),
        callback_address=_get("NEW_API_CALLBACK_ADDRESS", f"https://{domain}").rstrip("/"),
        notify_url=_get("NEW_API_NOTIFY_URL", ""),
    )


def load_smtp_config() -> SmtpConfig:
    return SmtpConfig(
        host=_get("SMTP_HOST", ""),
        port=int_env("SMTP_PORT", 587),
        username=_get("SMTP_USERNAME", ""),
        password=_get("SMTP_PASSWORD", ""),
        sender=_get("SMTP_FROM", ""),
        ssl=bool_env("SMTP_SSL", False),
        force_auth_login=bool_env("SMTP_FORCE_AUTH_LOGIN", False),
    )


def load_site_config() -> SiteConfig:
    return SiteConfig(
        system_name=_get("NEW_API_SYSTEM_NAME", "Crypto LLM"),
        logo_text=_get("NEW_API_LOGO_TEXT", "Crypto LLM"),
        register_enabled=bool_env("REGISTER_ENABLED", True),
        password_register_enabled=bool_env("PASSWORD_REGISTER_ENABLED", True),
        email_verification_enabled=bool_env("EMAIL_VERIFICATION_ENABLED", True),
    )


def _split_models(raw: str) -> list:
    if not raw:
        return []
    return [m.strip() for m in raw.split(",") if m.strip()]


def load_channels_config() -> list:
    """读取 OpenAI 兼容渠道配置（1~2 个）。"""
    channels: list = []
    text_models = _split_models(_get("TEXT_MODELS", ""))
    for idx in (1, 2):
        name = _get(f"OPENAI_CHANNEL_{idx}_NAME", "")
        if not name:
            continue
        channels.append(
            ChannelConfig(
                name=name,
                type=int_env(f"OPENAI_CHANNEL_{idx}_TYPE", 1),
                base_url=_get(f"OPENAI_CHANNEL_{idx}_BASE_URL", "https://api.openai.com"),
                key=_get(f"OPENAI_CHANNEL_{idx}_KEY", ""),
                models=_split_models(_get(f"OPENAI_CHANNEL_{idx}_MODELS", "")) or text_models,
                group=_get(f"OPENAI_CHANNEL_{idx}_GROUP", "default"),
                model_mapping=_get(f"OPENAI_CHANNEL_{idx}_MODEL_MAPPING", ""),
            )
        )
    return channels


def load_doubao_channel() -> ChannelConfig:
    default_models = [
        "doubao-seedance-1-0-pro-250528",
        "doubao-seedance-1-0-lite-t2v",
        "doubao-seedance-1-0-lite-i2v",
        "doubao-seedance-1-5-pro-251215",
        "doubao-seedream-4-0-250828",
    ]
    return ChannelConfig(
        name=_get("DOUBAO_CHANNEL_NAME", "doubao-video"),
        type=54,
        base_url=_get("DOUBAO_CHANNEL_BASE_URL", "https://ark.cn-beijing.volces.com"),
        key=_get("ARK_API_KEY", ""),
        models=_split_models(_get("DOUBAO_CHANNEL_MODELS", "")) or default_models,
        group=_get("DOUBAO_CHANNEL_GROUP", "default"),
    )


def load_reconcile_config() -> ReconcileConfig:
    wallets = [
        w.strip()
        for w in _get("WALLET_ADDRESSES", "").replace("\n", ",").split(",")
        if w.strip()
    ]
    return ReconcileConfig(
        adapter_pg_dsn=_get("ADAPTER_PG_DSN", "") or _get("DATABASE_URL", ""),
        new_api=load_new_api_config(),
        epusdt_base_url=_get("EPUSDT_BASE_URL", "").rstrip("/"),
        epusdt_admin_username=_get("EPUSDT_ADMIN_USERNAME", "admin"),
        epusdt_admin_password=_get("EPUSDT_ADMIN_PASSWORD", ""),
        trongrid_api_key=_get("TRONGRID_API_KEY", ""),
        etherscan_api_key=_get("ETHERSCAN_API_KEY", ""),
        usdc_polygon_contract=_get("USDC_POLYGON_CONTRACT", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"),
        quota_per_usd=int_env("QUOTA_PER_USD", 500000),
        rate_max_age_seconds=int_env("RATE_MAX_AGE_SECONDS", 300),
        wallet_addresses=wallets,
        report_dir=_get("REPORT_DIR", "reports"),
        tg_bot_token=_get("TG_BOT_TOKEN", "") or _get("TELEGRAM_BOT_TOKEN", ""),
        tg_chat_id=_get("TG_CHAT_ID", "") or _get("TELEGRAM_CHAT_ID", ""),
    )
