from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache


class ConfigurationError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"missing required environment variable: {name}")
    return value


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    new_api_database_url: str
    epay_md5_key: str
    gmpay_secret_key: str
    epay_hmac_key: str
    gmpay_pid: str
    epay_pid: str
    epusdt_container_ip: str
    epusdt_base_url: str
    epusdt_admin_username: str
    epusdt_admin_password: str
    new_api_notify_url: str
    quota_per_usd: Decimal
    rate_max_age_seconds: int = 300
    http_timeout_seconds: float = 10.0
    notify_retry_seconds: int = 60
    notify_background_retry: bool = True
    report_dir: str = "/tmp/payment-adapter-reports"
    reconcile_script: str = "/app/scripts/reconcile.py"
    reconcile_interval_seconds: int = 86400
    log_level: str = "INFO"
    trongrid_api_key: str | None = None
    etherscan_api_key: str | None = None
    usdc_polygon_contract: str = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"

    def __post_init__(self) -> None:
        if self.quota_per_usd <= 0:
            raise ConfigurationError("QUOTA_PER_USD must be positive")
        if self.rate_max_age_seconds <= 0:
            raise ConfigurationError("RATE_MAX_AGE_SECONDS must be positive")
        if self.notify_retry_seconds <= 0 or self.reconcile_interval_seconds <= 0:
            raise ConfigurationError("retry and reconcile intervals must be positive")
        object.__setattr__(self, "epusdt_base_url", self.epusdt_base_url.rstrip("/"))

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            quota = Decimal(_required("QUOTA_PER_USD"))
            rate_max_age = int(os.getenv("RATE_MAX_AGE_SECONDS", "300"))
            timeout = float(os.getenv("HTTP_TIMEOUT_SECONDS", "10"))
            notify_retry = int(os.getenv("NOTIFY_RETRY_SECONDS", "60"))
            reconcile_interval = int(os.getenv("RECONCILE_INTERVAL_SECONDS", "86400"))
        except (InvalidOperation, ValueError) as exc:
            raise ConfigurationError("invalid numeric environment configuration") from exc
        gmpay_pid = _required("GMPAY_PID")
        return cls(
            database_url=_required("DATABASE_URL"),
            new_api_database_url=_required("NEW_API_DATABASE_URL"),
            epay_md5_key=_required("EPAY_MD5_KEY"),
            gmpay_secret_key=_required("GMPAY_SECRET_KEY"),
            epay_hmac_key=_required("EPAY_HMAC_KEY"),
            gmpay_pid=gmpay_pid,
            epay_pid=os.getenv("EPAY_PID", "").strip() or gmpay_pid,
            epusdt_container_ip=_required("EPUSDT_CONTAINER_IP"),
            epusdt_base_url=os.getenv("EPUSDT_BASE_URL", "http://epusdt:8080"),
            epusdt_admin_username=_required("EPUSDT_ADMIN_USERNAME"),
            epusdt_admin_password=_required("EPUSDT_ADMIN_PASSWORD"),
            new_api_notify_url=_required("NEW_API_NOTIFY_URL"),
            quota_per_usd=quota,
            rate_max_age_seconds=rate_max_age,
            http_timeout_seconds=timeout,
            notify_retry_seconds=notify_retry,
            notify_background_retry=_bool("NOTIFY_BACKGROUND_RETRY", True),
            report_dir=os.getenv("REPORT_DIR", "/tmp/payment-adapter-reports"),
            reconcile_script=os.getenv("RECONCILE_SCRIPT", "/app/scripts/reconcile.py"),
            reconcile_interval_seconds=reconcile_interval,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            trongrid_api_key=os.getenv("TRONGRID_API_KEY", "").strip() or None,
            etherscan_api_key=os.getenv("ETHERSCAN_API_KEY", "").strip() or None,
            usdc_polygon_contract=(
                os.getenv("USDC_POLYGON_CONTRACT", "").strip()
                or "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
            ),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
