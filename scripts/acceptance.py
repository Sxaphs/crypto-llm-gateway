#!/usr/bin/env python3
"""验收测试：逐项实测 New API 七条关键路径（严格版）。

七项（默认 NOT RUN，实测成功才 PASS，失败 FAIL，缺输入 BLOCKED，绝不伪造）：
  1. email_register        邮箱注册（IMAP 取码或 --email-code），注册后真实 login 才 PASS
  2. order_payment         创建 5 USDT 订单，form POST 进 adapter/收银台，按 out_trade_no 查 epusdt，
                           等待真实付款，校验 quota 增量==adapter credit_quota、汇率源/tx/email/地址齐全
  3. openai_chat           OpenAI 兼容 chat，按 usage+pricing 比率核对扣费，并比较 quota
  4. seedance_video        Seedance 视频生成轮询，须含可访问视频 URL 且 quota 增量==model_price*500000
  5. epusdt_resend_callback 重放回调，断言 quota 不变
  6. restart               --restart 时 docker compose restart，检查数据（支付订单/topup 仍在）
  7. reconcile             调 scripts/reconcile.py（--no-chain 时不得 PASS）

证据写 acceptance/evidence/<id>-<UTC>.json；状态写 acceptance/status.json。
密钥、epay_params（含 MD5 签名）绝不写入 evidence。
"""
from __future__ import annotations

import argparse
import imaplib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Windows 控制台可能是 GBK；强制 UTF-8 输出，避免中文乱码。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import load_new_api_config, load_reconcile_config  # noqa: E402
from lib.epusdt import EpusdtAdminClient  # noqa: E402
from lib.newapi import NewApiClient  # noqa: E402
from lib.pg import read_payment_orders  # noqa: E402

STATUS_FILE = ROOT / "acceptance" / "status.json"
EVIDENCE_DIR = ROOT / "acceptance" / "evidence"
PRICING_PATH = ROOT / "pricing" / "model_ratios.json"

NOT_RUN = "NOT RUN"
BLOCKED = "BLOCKED"
PASS = "PASS"
FAIL = "FAIL"

QUOTA_PER_USD = 500000


def _env(key: str, default: str = "") -> str:
    import os  # noqa: PLC0415

    return os.environ.get(key, default)


def _int_env(key: str, default: int = 0) -> int:
    raw = _env(key, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        return default


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def write_evidence(item_id: str, payload: Dict[str, Any]) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / f"{item_id}-{utc_iso()}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def update_status(item_id: str, name: str, status: str, summary: str, evidence: str) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data: Dict[str, Any] = {}
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data.setdefault("items", {})
    data["items"][item_id] = {
        "name": name,
        "status": status,
        "summary": summary,
        "evidence": evidence,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- 通用 ----------------
def _http_json(url, *, method="GET", token="", payload=None, timeout=120) -> Tuple[int, Dict[str, Any]]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:1000]}
    except urllib.error.URLError as exc:
        raise RuntimeError(f"连接失败: {exc.reason}") from exc


def _form_post(url: str, params: Dict[str, str]) -> Tuple[int, Dict[str, str], str]:
    """form POST 到收银台/适配器提交端点，不自动跟随重定向，返回(status, headers, body)。"""
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect())
    try:
        resp = opener.open(req, timeout=30)
        return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")[:2000]
    except urllib.error.HTTPError as exc:
        hdrs = {k.lower(): v for k, v in exc.headers.items()}
        raw = exc.read().decode("utf-8", "replace")[:2000] if exc.fp else ""
        return exc.code, hdrs, raw


def _new_api_admin() -> NewApiClient:
    cfg = load_new_api_config()
    client = NewApiClient(
        cfg.base_url,
        basic_auth_username=cfg.basic_auth_username,
        basic_auth_password=cfg.basic_auth_password,
    )
    client.connect(cfg.admin_username, cfg.admin_password)
    return client


def _login_as_user(username: str, password: str) -> NewApiClient:
    cfg = load_new_api_config()
    client = NewApiClient(
        cfg.base_url,
        basic_auth_username=cfg.basic_auth_username,
        basic_auth_password=cfg.basic_auth_password,
    )
    client.connect(username, password)
    return client


def _get_user_quota(admin: NewApiClient, user_id: int) -> Optional[int]:
    user = admin.get_user(user_id)
    if not user:
        return None
    return int(user.get("quota", 0) or 0)


def _load_pricing() -> Dict[str, Any]:
    return json.loads(PRICING_PATH.read_text(encoding="utf-8"))


def _read_adapter_order(out_trade_no: str) -> Optional[Dict[str, Any]]:
    cfg = load_reconcile_config()
    if not cfg.adapter_pg_dsn:
        return None
    rows = read_payment_orders(cfg.adapter_pg_dsn, since=int(time.time()) - 86400, until=int(time.time()))["rows"]
    for r in rows:
        if str(r.get("out_trade_no", "") or "") == out_trade_no:
            return r
    return None


def _get_relay_token(args) -> Tuple[str, str, Dict[str, Any]]:
    """返回 (token, source, meta)。meta 不含完整 key。"""
    token = _env("ACCEPTANCE_API_TOKEN", "")
    if token:
        return token, "env", {"source": "ACCEPTANCE_API_TOKEN"}
    username = _env("EMAIL_REGISTER_USERNAME", "") or _env("NEW_API_ADMIN_USERNAME", "root")
    password = _env("EMAIL_REGISTER_PASSWORD", "") or _env("NEW_API_ADMIN_PASSWORD", "")
    if not password:
        return "", "blocked", {"reason": "无 ACCEPTANCE_API_TOKEN 且无登录凭据"}
    client = _login_as_user(username, password)
    name = f"acceptance-{int(time.time())}"
    client.create_token(name, unlimited_quota=True)
    tokens = client.list_tokens()
    tid = None
    for t in tokens:
        if t.get("name") == name:
            tid = int(t.get("id", 0) or 0)
            break
    if tid is None and tokens:
        tid = max(int(t.get("id", 0) or 0) for t in tokens)
    if not tid:
        return "", "blocked", {"reason": "创建后未找到新 token id"}
    key = client.get_token_key(tid)
    if not key:
        return "", "blocked", {"reason": "无法取回 token key"}
    return key, "created", {
        "token_id": tid,
        "token_name": name,
        "key_prefix": key[:8],
        "user_id": client.user_id,
    }


def _masked_key(token: str) -> str:
    return token[:8] + "..." if token else ""


# ---------------- 各项 ----------------
def item_email_register(args) -> Tuple[str, str, Dict[str, Any]]:
    base = load_new_api_config().base_url
    email = _env("EMAIL_REGISTER_ADDRESS", "")
    username = _env("EMAIL_REGISTER_USERNAME", "")
    password = _env("EMAIL_REGISTER_PASSWORD", "")
    if not (email and username and password):
        return BLOCKED, "缺 EMAIL_REGISTER_ADDRESS / EMAIL_REGISTER_USERNAME / EMAIL_REGISTER_PASSWORD", {}

    code, body = _http_json(f"{base}/api/verification?{urllib.parse.urlencode({'email': email})}")
    if code != 200 or body.get("success") is not True:
        return BLOCKED, f"发送验证码失败: HTTP {code} {body.get('message', '')}", {"send": body}

    vcode = args.email_code or ""
    if not vcode:
        imap_host = _env("IMAP_HOST", "")
        imap_port = _int_env("IMAP_PORT", 993)
        imap_user = _env("IMAP_USERNAME", "")
        imap_pass = _env("IMAP_PASSWORD", "")
        if not (imap_host and imap_user and imap_pass):
            return BLOCKED, "缺验证码：请 --email-code 或配置 IMAP_HOST/IMAP_USERNAME/IMAP_PASSWORD", {}
        try:
            vcode = _imap_fetch_code(imap_host, imap_port, imap_user, imap_pass, email)
        except Exception as exc:  # noqa: BLE001
            return FAIL, f"IMAP 取码失败: {exc}", {}
    if not vcode:
        return FAIL, "未取得验证码", {}

    code, body = _http_json(
        f"{base}/api/user/register",
        method="POST",
        payload={"username": username, "password": password, "email": email, "verification_code": vcode},
    )
    if code != 200 or body.get("success") is not True:
        return FAIL, f"注册失败: HTTP {code} {body.get('message', '')}", {"body": body}

    # 严格：注册后真实 login 才 PASS
    try:
        c = _login_as_user(username, password)
        if c.user_id <= 0:
            return FAIL, "注册成功但登录返回空 user_id", {}
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"注册成功但随后登录失败: {exc}", {}
    import os as _os  # noqa: PLC0415

    _os.environ["ACCEPTANCE_USER_ID"] = str(c.user_id)
    return PASS, "邮箱注册成功且随后真实登录成功", {"email": email, "username": username, "user_id": c.user_id}


def _imap_fetch_code(host: str, port: int, user: str, password: str, email: str) -> str:
    m = imaplib.IMAP4_SSL(host, port)
    try:
        m.login(user, password)
        m.select("INBOX")
        typ, data = m.search(None, f'(TO "{email}")')
        if typ != "OK" or not data or not data[0]:
            typ, data = m.search(None, "ALL")
        ids = data[0].split()[-20:] if data and data[0] else []
        for num in reversed(ids):
            typ, msg_data = m.fetch(num, "(RFC822)")
            if typ != "OK":
                continue
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else b""
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            found = re.findall(r"\b(\d{6})\b", text)
            if found:
                return found[-1]
        return ""
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass


def item_order_payment(args) -> Tuple[str, str, Dict[str, Any]]:
    cfg = load_new_api_config()
    username = _env("EMAIL_REGISTER_USERNAME", "")
    password = _env("EMAIL_REGISTER_PASSWORD", "")
    if not (username and password):
        return BLOCKED, "缺下单账号 EMAIL_REGISTER_USERNAME / EMAIL_REGISTER_PASSWORD", {}

    admin = None
    user_id = None
    try:
        admin = _new_api_admin()
        user_client = _login_as_user(username, password)
        user_id = user_client.user_id
        quota_before = _get_user_quota(admin, user_id)
        if quota_before is None:
            return BLOCKED, "无法读取下单账号 quota（缺 user/admin quota 证据）", {}
    except Exception as exc:  # noqa: BLE001
        return BLOCKED, f"登录失败: {exc}", {}

    try:
        resp = user_client.request_epay(5, "usdt.tron")
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"创建订单失败: {exc}", {}

    pay_url = str(resp.get("url", "") or "")
    params = resp.get("data") or {}
    if not isinstance(params, dict) or not pay_url:
        return FAIL, "New API 未返回 params/url", {}

    out_trade_no = str(params.get("out_trade_no", "") or "")
    if not out_trade_no:
        return FAIL, "params 缺 out_trade_no", {}
    # 串联：供后续 restart 项使用（只写环境变量，不进 evidence）
    import os as _os  # noqa: PLC0415

    _os.environ["EPUSDT_ORDER_OUT_TRADE_NO"] = out_trade_no

    # form POST 进 adapter/收银台（不记录 epay_params，避免 MD5 签名泄漏）
    status, headers, body = _form_post(pay_url, params)
    location = headers.get("location", "")
    if status != 302 or not str(location).lower().startswith(("http://", "https://")):
        return FAIL, f"form POST 未返回 302 重定向到有效收银台: status={status} location={location!r}", {"out_trade_no": out_trade_no}

    # 按 out_trade_no keyword 查 epusdt（不拿最近单）
    epusdt_cfg = load_reconcile_config()
    epusdt_order = None
    if epusdt_cfg.epusdt_base_url and epusdt_cfg.epusdt_admin_password:
        try:
            ec = EpusdtAdminClient(epusdt_cfg.epusdt_base_url)
            ec.login(epusdt_cfg.epusdt_admin_username, epusdt_cfg.epusdt_admin_password)
            data = ec.list_orders(keyword=out_trade_no, page=1, page_size=20)
            for o in data["list"]:
                if str(o.get("order_id", "") or "") == out_trade_no or str(o.get("trade_id", "") or "") == out_trade_no:
                    epusdt_order = o
                    break
        except Exception as exc:  # noqa: BLE001
            pass

    if epusdt_order is None:
        return FAIL, "未能在 epusdt 按 out_trade_no 找到订单", {"out_trade_no": out_trade_no}

    trade_id = str(epusdt_order.get("trade_id", "") or "")
    receive_address = str(epusdt_order.get("receive_address", "") or "")
    if trade_id:
        _os.environ["EPUSDT_ORDER_TRADE_ID"] = trade_id
    info = {
        "out_trade_no": out_trade_no,
        "epusdt_trade_id": trade_id,
        "receive_address": receive_address,
        "cashier_location": location,
        "form_post_status": status,
    }
    print(f"[order_payment] 收银台: {location or pay_url}")
    print(f"[order_payment] 收款地址: {receive_address or '(见收银台)'}")

    timeout = args.payment_timeout
    if timeout <= 0:
        return BLOCKED, "已创建订单并 form POST 进收银台，等待真实付款（--payment-timeout>0 轮询）", info

    if not (epusdt_cfg.epusdt_base_url and epusdt_cfg.epusdt_admin_password and trade_id):
        return BLOCKED, "缺 epusdt 管理端配置/trade_id，无法轮询", info

    ec = EpusdtAdminClient(epusdt_cfg.epusdt_base_url)
    ec.login(epusdt_cfg.epusdt_admin_username, epusdt_cfg.epusdt_admin_password)
    deadline = time.time() + timeout
    while time.time() < deadline:
        order = ec.get_order(trade_id)
        if order and int(order.get("status", 0) or 0) == 2:
            break
        time.sleep(10)
    else:
        return FAIL, f"等待真实付款超时（{timeout}s）", info

    # 等 New API topup success（超时明确 FAIL）
    newapi_success = False
    deadline2 = time.time() + 180
    while time.time() < deadline2:
        for t in admin.list_topups():
            if str(t.get("trade_no", "") or "") == out_trade_no and str(t.get("status", "") or "") == "success":
                newapi_success = True
                break
        if newapi_success:
            break
        time.sleep(3)
    if not newapi_success:
        return FAIL, f"等待 New API topup success 超时（180s）", info

    # 读 adapter 结算
    adapter_order = _read_adapter_order(out_trade_no)
    if adapter_order is None:
        return FAIL, "adapter 无此 out_trade_no 结算记录", info

    credit_quota = adapter_order.get("credit_quota")
    quota_after = _get_user_quota(admin, user_id)
    info["quota_before"] = quota_before
    info["quota_after"] = quota_after
    info["adapter_status"] = adapter_order.get("status")
    info["adapter_credit_quota"] = credit_quota
    info["adapter_email_present"] = bool(adapter_order.get("email"))
    info["adapter_tx_present"] = bool(adapter_order.get("block_transaction_id"))
    info["adapter_rate_source"] = adapter_order.get("rate_source")
    info["adapter_receive_address_present"] = bool(adapter_order.get("receive_address"))
    info["adapter_payer_address_present"] = bool(adapter_order.get("payer_address"))
    info["adapter_payer_address_pending"] = bool(adapter_order.get("payer_address_pending"))
    info["adapter_confirmed_at_present"] = bool(adapter_order.get("confirmed_at"))
    info["adapter_rate_observed_at_present"] = bool(adapter_order.get("rate_observed_at"))
    info["adapter_settled_usd"] = str(adapter_order.get("settled_usd") or "")

    # 断言：quota 增量 == credit_quota；汇率源/tx/email/地址/结算时间齐全，payer 已确定
    missing = []
    if not adapter_order.get("email"):
        missing.append("email")
    if not adapter_order.get("block_transaction_id"):
        missing.append("tx")
    if not adapter_order.get("rate_source"):
        missing.append("rate_source")
    if not adapter_order.get("receive_address"):
        missing.append("receive_address")
    if not adapter_order.get("payer_address"):
        missing.append("payer_address")
    if not adapter_order.get("confirmed_at"):
        missing.append("confirmed_at")
    if not adapter_order.get("rate_observed_at"):
        missing.append("rate_observed_at")
    if bool(adapter_order.get("payer_address_pending")):
        missing.append("payer_address_pending=true")
    if missing:
        return FAIL, f"adapter 结算字段缺失/异常: {', '.join(missing)}", info

    if quota_after is None or credit_quota is None:
        return FAIL, "缺 quota/credit_quota 证据", info
    delta = quota_after - quota_before
    info["quota_delta"] = delta
    if delta != int(credit_quota):
        return FAIL, f"quota 增量({delta}) != credit_quota({int(credit_quota)})", info
    return PASS, "真实付款完成：quota 增量==credit_quota，汇率源/tx/email/地址/payer/结算时间齐全", info


def item_openai_chat(args) -> Tuple[str, str, Dict[str, Any]]:
    base = load_new_api_config().base_url
    token, source, meta = _get_relay_token(args)
    if not token:
        return BLOCKED, f"缺 relay token: {meta.get('reason')}", {}
    model = _env("ACCEPTANCE_CHAT_MODEL", "gpt-4o-mini")
    user_id = _int_env("ACCEPTANCE_USER_ID", 0) or int(meta.get("user_id", 0) or 0)

    # 必须用 OpenAI SDK
    try:
        from openai import OpenAI  # noqa: PLC0415
    except ImportError:
        return BLOCKED, "未安装 openai SDK，请先: pip install -r scripts/requirements.txt", {}

    admin = None
    quota_before = None
    if user_id and _env("NEW_API_ADMIN_PASSWORD", ""):
        try:
            admin = _new_api_admin()
            quota_before = _get_user_quota(admin, user_id)
        except Exception:  # noqa: BLE001
            admin = None
    if user_id == 0 or quota_before is None:
        return BLOCKED, "缺 ACCEPTANCE_USER_ID 或无法读取 quota（不允许无 quota 证据 PASS）", {"source": source, **meta}

    pricing = _load_pricing()
    model_ratio = pricing.get("model_ratio", {}).get(model)
    completion_ratio = pricing.get("completion_ratio", {}).get(model, 1.0)
    if model_ratio is None:
        return BLOCKED, f"{model} 不在 pricing/model_ratios.json 的 model_ratio，无法核对扣费", {}

    client = OpenAI(api_key=token, base_url=base.rstrip("/") + "/v1")
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping, reply exactly: pong"}],
            max_tokens=16,
        )
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"OpenAI SDK chat 调用失败: {exc}", {}

    usage = resp.usage.model_dump() if getattr(resp, "usage", None) else {}
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    raw = (prompt_tokens + completion_tokens * completion_ratio) * model_ratio
    expected = int(Decimal(str(raw)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    quota_after = _get_user_quota(admin, user_id)
    detail = {
        "source": source,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "model_ratio": model_ratio,
        "completion_ratio": completion_ratio,
        "expected_quota": expected,
        "quota_before": quota_before,
        "quota_after": quota_after,
    }
    detail.update({k: v for k, v in meta.items() if k != "key"})
    if quota_after is None:
        return FAIL, "缺扣费后 quota 证据", detail
    actual = quota_before - quota_after
    detail["quota_delta"] = actual
    if actual <= 0:
        return FAIL, "chat 成功但 quota 未扣减", detail
    if actual != expected:
        return FAIL, f"扣费与 pricing 比率不符: actual={actual} expected={expected}", detail
    return PASS, "OpenAI SDK chat 成功，扣费与 pricing 比率一致", detail


def item_seedance_video(args) -> Tuple[str, str, Dict[str, Any]]:
    base = load_new_api_config().base_url
    token, source, meta = _get_relay_token(args)
    if not token:
        return BLOCKED, f"缺 relay token: {meta.get('reason')}", {}
    model = _env("ACCEPTANCE_VIDEO_MODEL", "doubao-seedance-1-0-lite-t2v")
    user_id = _int_env("ACCEPTANCE_USER_ID", 0) or int(meta.get("user_id", 0) or 0)

    admin = None
    quota_before = None
    if user_id and _env("NEW_API_ADMIN_PASSWORD", ""):
        try:
            admin = _new_api_admin()
            quota_before = _get_user_quota(admin, user_id)
        except Exception:  # noqa: BLE001
            admin = None
    if user_id == 0 or quota_before is None:
        return BLOCKED, "缺 ACCEPTANCE_USER_ID 或无法读取 quota（不允许无 quota 证据 PASS）", {}

    pricing = _load_pricing()
    model_price = pricing.get("model_price", {}).get(model)
    if model_price is None:
        return BLOCKED, f"{model} 不在 pricing/model_ratios.json 的 model_price，无法核对扣费", {}

    code, body = _http_json(
        f"{base}/v1/video/generations",
        method="POST",
        token=token,
        payload={"model": model, "prompt": "a rotating cube", "seconds": "5"},
        timeout=120,
    )
    if code != 200:
        return FAIL, f"视频生成请求失败: HTTP {code}", body
    task_id = body.get("id") or body.get("task_id") or ""
    if not task_id:
        return FAIL, "视频生成响应缺 task_id", body

    final: Dict[str, Any] = {}
    deadline = time.time() + _int_env("VIDEO_POLL_TIMEOUT", 300)
    while time.time() < deadline:
        c2, b2 = _http_json(f"{base}/v1/video/generations/{task_id}", method="GET", token=token, timeout=60)
        final = b2
        if b2.get("status") in ("completed", "succeeded", "failed"):
            break
        time.sleep(5)

    status = final.get("status", "")
    content = final.get("content") or {}
    video_url = ""
    if isinstance(content, dict):
        video_url = str(content.get("video_url") or "")
    if not video_url:
        video_url = (final.get("metadata") or {}).get("url") or final.get("url") or ""
    detail = {"source": source, "task_id": task_id, "status": status, "video_url": video_url}
    detail.update(meta)
    if status not in ("completed", "succeeded"):
        return FAIL, f"视频生成未完成（status={status}）", detail
    if not video_url:
        return FAIL, "视频完成但缺可访问 URL", detail
    # 校验 URL 可访问
    try:
        req = urllib.request.Request(video_url, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            detail["video_url_status"] = r.status
            if r.status != 200:
                return FAIL, f"视频 URL 不可访问: HTTP {r.status}", detail
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"视频 URL 不可访问: {exc}", detail

    quota_after = _get_user_quota(admin, user_id)
    detail["quota_before"] = quota_before
    detail["quota_after"] = quota_after
    expected = int(Decimal(str(model_price)) * Decimal(str(QUOTA_PER_USD)))
    detail["expected_quota"] = expected
    if quota_after is None:
        return FAIL, "缺扣费后 quota 证据", detail
    actual = quota_before - quota_after
    detail["quota_delta"] = actual
    if actual != expected:
        return FAIL, f"视频扣费不符: actual={actual} expected={expected}（group ratio 应为 1）", detail
    return PASS, "Seedance 视频生成成功，URL 可访问，扣费==model_price*500000", detail


def item_epusdt_resend_callback(args) -> Tuple[str, str, Dict[str, Any]]:
    epusdt_cfg = load_reconcile_config()
    trade_id = _env("EPUSDT_ORDER_TRADE_ID", "")
    user_id = _int_env("ACCEPTANCE_USER_ID", 0)
    if not (epusdt_cfg.epusdt_base_url and epusdt_cfg.epusdt_admin_password):
        return BLOCKED, "缺 EPUSDT_BASE_URL / EPUSDT_ADMIN_PASSWORD", {}
    if not trade_id:
        return BLOCKED, "缺 EPUSDT_ORDER_TRADE_ID（已支付 epusdt 订单）", {}

    admin = None
    quota_before = None
    if user_id and _env("NEW_API_ADMIN_PASSWORD", ""):
        try:
            admin = _new_api_admin()
            quota_before = _get_user_quota(admin, user_id)
        except Exception:  # noqa: BLE001
            admin = None
    if user_id == 0 or quota_before is None:
        return BLOCKED, "缺 ACCEPTANCE_USER_ID 或无法读取 quota（不允许无 quota 证据 PASS）", {}

    ec = EpusdtAdminClient(epusdt_cfg.epusdt_base_url)
    try:
        ec.login(epusdt_cfg.epusdt_admin_username, epusdt_cfg.epusdt_admin_password)
        ec.resend_callback(trade_id)
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"resend-callback 失败: {exc}", {"trade_id": trade_id}

    time.sleep(8)
    quota_after = _get_user_quota(admin, user_id)
    detail = {"trade_id": trade_id, "quota_before": quota_before, "quota_after": quota_after}
    if quota_after is None:
        return FAIL, "缺重放后 quota 证据", detail
    if quota_before != quota_after:
        return FAIL, "resend-callback 重放导致 quota 变化（应为幂等不变）", detail
    return PASS, "resend-callback 重放成功且 quota 不变", detail


def item_restart(args) -> Tuple[str, str, Dict[str, Any]]:
    if not args.restart:
        return BLOCKED, "未指定 --restart", {}
    project_dir = _env("COMPOSE_PROJECT_DIR", "")
    if not project_dir:
        return BLOCKED, "缺 COMPOSE_PROJECT_DIR", {}
    base = load_new_api_config().base_url
    user_id = _int_env("ACCEPTANCE_USER_ID", 0)
    out_trade_no = _env("EPUSDT_ORDER_OUT_TRADE_NO", "")

    admin = None
    quota_before = None
    topup_before = None
    if user_id and _env("NEW_API_ADMIN_PASSWORD", ""):
        try:
            admin = _new_api_admin()
            quota_before = _get_user_quota(admin, user_id)
            for t in admin.list_topups():
                if str(t.get("trade_no", "") or "") == out_trade_no:
                    topup_before = t
                    break
        except Exception:  # noqa: BLE001
            admin = None
    if user_id == 0 or quota_before is None:
        return BLOCKED, "缺 ACCEPTANCE_USER_ID 或无法读取 quota（不允许无 quota 证据 PASS）", {}
    if not out_trade_no:
        return BLOCKED, "缺 EPUSDT_ORDER_OUT_TRADE_NO（不得 PASS）", {}
    if topup_before is None:
        return FAIL, f"EPUSDT_ORDER_OUT_TRADE_NO={out_trade_no} 对应 topup 在重启前不存在（不得 PASS）", {"out_trade_no": out_trade_no}

    try:
        subprocess.run(
            ["docker", "compose", "restart"],
            cwd=project_dir, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"docker compose restart 失败: {exc}", {}

    healthy = False
    for _ in range(30):
        try:
            code, _ = _http_json(f"{base}/api/status", timeout=5)
            if code == 200:
                healthy = True
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    if not healthy:
        return FAIL, "重启后 New API 未恢复健康", {}

    quota_after = _get_user_quota(admin, user_id)
    topup_after = None
    for t in admin.list_topups():
        if str(t.get("trade_no", "") or "") == out_trade_no:
            topup_after = t
            break
    adapter_order = _read_adapter_order(out_trade_no) if out_trade_no else None

    detail = {
        "quota_before": quota_before,
        "quota_after": quota_after,
        "topup_exists_before": topup_before is not None,
        "topup_exists_after": topup_after is not None,
        "adapter_order_exists_after": adapter_order is not None,
    }
    if quota_before != quota_after:
        return FAIL, "重启后 quota 变化", detail
    if out_trade_no and (topup_before is not None and topup_after is None):
        return FAIL, "重启后 topup 订单丢失", detail
    if out_trade_no and adapter_order is None:
        return FAIL, "重启后 adapter 支付订单丢失", detail
    return PASS, "重启成功且数据一致（quota/topup/支付订单仍在）", detail


def item_reconcile(args) -> Tuple[str, str, Dict[str, Any]]:
    cmd = [sys.executable, str(ROOT / "scripts" / "reconcile.py")]
    if args.no_chain:
        cmd.append("--no-chain")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"reconcile 执行异常: {exc}", {}
    detail = {"exit_code": proc.returncode, "stdout": (proc.stdout or "")[-4000:], "stderr": (proc.stderr or "")[-2000:]}
    if args.no_chain:
        return BLOCKED, "--no-chain 的 reconcile 不得 PASS（标记 BLOCKED）", detail
    if proc.returncode == 0:
        return PASS, "对账通过（FULL PASS）", detail
    if proc.returncode == 1:
        return FAIL, "对账发现差异", detail
    return BLOCKED, f"对账运行错误（exit={proc.returncode}，检查配置）", detail


ITEMS = {
    "email_register": ("邮箱注册（IMAP/--email-code，注册后真实登录）", item_email_register),
    "order_payment": ("创建 5 USDT 订单并等待真实付款", item_order_payment),
    "openai_chat": ("OpenAI 兼容 chat（usage+pricing 核对扣费）", item_openai_chat),
    "seedance_video": ("Seedance 视频生成轮询（URL 可访问 + 扣费核对）", item_seedance_video),
    "epusdt_resend_callback": ("epusdt resend-callback 重放 + quota 不变", item_epusdt_resend_callback),
    "restart": ("docker compose restart + 数据检查", item_restart),
    "reconcile": ("三方对账", item_reconcile),
}


def run(args) -> int:
    if args.list:
        for iid, (name, _) in ITEMS.items():
            print(f"{iid}: {name}")
        return 0

    ids = [args.only] if args.only else list(ITEMS.keys())
    if args.only and args.only not in ITEMS:
        print(f"未知项: {args.only}；可用: {', '.join(ITEMS)}", file=sys.stderr)
        return 2

    rc = 0
    for iid in ids:
        name, fn = ITEMS[iid]
        try:
            status, summary, detail = fn(args)
        except Exception as exc:  # noqa: BLE001
            status, summary, detail = FAIL, f"未捕获异常: {exc}", {}
        detail = dict(detail or {})
        detail["_status"] = status
        detail["_summary"] = summary
        evidence = str(write_evidence(iid, detail))
        update_status(iid, name, status, summary, evidence)
        print(f"[{iid}] {status}: {summary}")
        if status == FAIL:
            rc = 1
        elif status == BLOCKED:
            rc = 2
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--list", action="store_true")
    p.add_argument("--only")
    p.add_argument("--all", action="store_true")
    p.add_argument("--email-code")
    p.add_argument("--payment-timeout", type=int, default=0)
    p.add_argument("--restart", action="store_true")
    p.add_argument("--no-chain", action="store_true")
    return p


if __name__ == "__main__":
    sys.exit(run(build_parser().parse_args()))
