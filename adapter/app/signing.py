from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from typing import Any, Mapping


class SignatureValueError(ValueError):
    pass


def decimal_to_plain(value: Decimal) -> str:
    """Match epusdt's non-exponent JSON number canonicalization without float loss."""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    if not value.is_finite():
        raise SignatureValueError("non-finite numbers cannot be signed")
    if value.adjusted() > 10000 or value.adjusted() < -10000:
        raise SignatureValueError("number exponent is too large")
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def signature_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise SignatureValueError("booleans cannot be signed")
    if isinstance(value, Decimal):
        return decimal_to_plain(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Inbound JSON is parsed as Decimal. This branch is for internal callers.
        return decimal_to_plain(Decimal(str(value)))
    if isinstance(value, str):
        return value if value != "" else None
    raise SignatureValueError(f"unsupported signature value type: {type(value).__name__}")


def canonical_params(params: Mapping[str, Any], excluded: frozenset[str]) -> str:
    pairs: list[tuple[str, str]] = []
    for key, raw_value in params.items():
        if key in excluded:
            continue
        if not isinstance(key, str):
            raise SignatureValueError("parameter names must be strings")
        value = signature_value(raw_value)
        if value is not None:
            pairs.append((key, value))
    pairs.sort(key=lambda pair: pair[0].encode("utf-8"))
    return "&".join(f"{key}={value}" for key, value in pairs)


def gmpay_hmac(params: Mapping[str, Any], secret: str) -> str:
    canonical = canonical_params(params, frozenset({"signature"}))
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def epay_md5(params: Mapping[str, Any], secret: str) -> str:
    canonical = canonical_params(params, frozenset({"sign", "sign_type"}))
    return hashlib.md5((canonical + secret).encode("utf-8"), usedforsecurity=False).hexdigest()


def signatures_equal(expected: str, supplied: Any) -> bool:
    return isinstance(supplied, str) and hmac.compare_digest(expected, supplied)
