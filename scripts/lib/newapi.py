"""New API (QuantumNous/new-api v1.0.0-rc.49) 官方 HTTP API 客户端。

仅用标准库 urllib + http.cookiejar。字段/鉴权/路由均已对照 v1.0.0-rc.49 源码核实：

- 登录:      POST /api/user/login   {username,password} -> data.access_token（登录会话令牌）
- 鉴权:      所有 dashboard API 用 Authorization: Bearer <access_token>
             （rc.49 已移除 GET /api/user/token；不再使用 New-Api-User 头）
- 可选:      ADMIN_BASIC_AUTH_USERNAME/PASSWORD 存在时改用 HTTP Basic 走管理域
- 分页:      服务端 page_size 上限 100，本客户端自动按 100 翻页拉完整
- 系统选项:   GET/PUT /api/option/  (RootAuth)；支付合规需 POST /api/option/payment_compliance
- 渠道:       GET/POST /api/channel/  PUT /api/channel/（update 不能带 status 字段）
- 令牌:       POST /api/token/ 创建，GET /api/token/ 列表(掩码)，POST /api/token/:id/key 取完整 key
- 用户/充值/日志: GET /api/user/ /api/user/topup /api/log/ (AdminAuth)

密钥绝不打印：本模块异常信息都不包含 key/password/token 原文。
"""
from __future__ import annotations

import base64
import http.cookiejar
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


class NewApiError(RuntimeError):
    """New API 请求失败。message 不含密钥。"""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body[:2000] if body else ""


class NewApiClient:
    def __init__(
        self,
        base_url: str,
        timeout: int = 30,
        basic_auth_username: str = "",
        basic_auth_password: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.access_token: str = ""
        self.user_id: int = 0
        self.role: int = 0
        self.basic_auth_username = basic_auth_username
        self.basic_auth_password = basic_auth_password
        self._jar = http.cookiejar.CookieJar()
        self._ctx = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar),
            urllib.request.HTTPSHandler(context=self._ctx),
        )

    # ---------- 低层请求 ----------
    def _auth_header(self) -> str:
        # Bearer 优先：拿到 access_token 后管理网关保留原生 Bearer；
        # Basic 仅用于尚无 token 的初始请求（如登录前经管理域）。
        if self.access_token:
            return "Bearer " + self.access_token
        if self.basic_auth_username:
            raw = f"{self.basic_auth_username}:{self.basic_auth_password}".encode("utf-8")
            return "Basic " + base64.b64encode(raw).decode("ascii")
        return ""

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[Any] = None,
        *,
        allow_redirects: bool = True,
    ) -> Dict[str, Any]:
        url = self.base_url + path
        body = None
        headers = {"Accept": "application/json"}
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        auth = self._auth_header()
        if auth:
            headers["Authorization"] = auth

        if not allow_redirects:
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(self._jar),
                urllib.request.HTTPSHandler(context=self._ctx),
                NoRedirectHandler(),
            )
        else:
            opener = self._opener

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            resp = opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
            raise NewApiError(f"HTTP {exc.code} {method} {path}", exc.code, raw) from exc
        except urllib.error.URLError as exc:
            raise NewApiError(f"连接失败 {method} {path}: {exc.reason}") from exc

        raw = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise NewApiError(f"响应不是 JSON {method} {path}") from exc

    def _ok(self, resp: Dict[str, Any], path: str) -> Dict[str, Any]:
        if resp.get("success") is True:
            return resp
        msg = str(resp.get("message", "unknown"))[:500]
        raise NewApiError(f"{path} 失败: {msg}")

    def _list_all(self, path: str, item_key: str) -> List[Dict[str, Any]]:
        """按 page_size=100 翻页拉取完整列表。"""
        base = path.rstrip("?&")
        items: List[Dict[str, Any]] = []
        page = 1
        while True:
            sep = "&" if "?" in base else "?"
            paged = f"{base}{sep}p={page}&page_size=100"
            resp = self._request("GET", paged)
            self._ok(resp, path)
            data = resp.get("data") or {}
            batch = data.get(item_key) or data.get("items") or []
            if isinstance(batch, dict):
                batch = batch.get("items") or []
            if not batch:
                break
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
            if page > 10000:
                break
        return items

    # ---------- 登录 ----------
    def login(self, username: str, password: str) -> None:
        resp = self._request(
            "POST", "/api/user/login", {"username": username, "password": password}
        )
        self._ok(resp, "/api/user/login")
        data = resp.get("data") or {}
        token = str(data.get("access_token", "") or "")
        if not token:
            # 可能需要 2FA/验证挑战，或账号被禁
            raise NewApiError("登录未返回 access_token（可能要求 2FA/验证挑战）")
        self.access_token = token
        user = data.get("user") or {}
        self.user_id = int(user.get("id", 0) or 0)
        self.role = int(user.get("role", 0) or 0)

    def connect(self, username: str, password: str) -> "NewApiClient":
        """登录并复用登录会话令牌。rc.49 已无独立取令牌接口。"""
        self.login(username, password)
        return self

    # ---------- 系统选项（RootAuth） ----------
    def get_options(self) -> List[Dict[str, Any]]:
        resp = self._request("GET", "/api/option/")
        self._ok(resp, "/api/option/")
        return resp.get("data") or []

    def put_option(self, key: str, value: Any) -> None:
        if isinstance(value, str):
            str_value = value
        elif isinstance(value, bool):
            str_value = "true" if value else "false"
        elif isinstance(value, (int, float)):
            str_value = repr(value)
        else:
            str_value = json.dumps(value, ensure_ascii=False)
        resp = self._request("PUT", "/api/option/", {"key": key, "value": str_value})
        self._ok(resp, "/api/option/")

    def confirm_payment_compliance(self) -> Dict[str, Any]:
        """POST /api/option/payment_compliance，需 dashboard session（非 PAT）。"""
        resp = self._request("POST", "/api/option/payment_compliance", {"confirmed": True})
        self._ok(resp, "/api/option/payment_compliance")
        return resp.get("data") or {}

    # ---------- 渠道（AdminAuth） ----------
    def list_channels(self) -> List[Dict[str, Any]]:
        return self._list_all("/api/channel/?id_sort=true", "items")

    def get_channel(self, channel_id: int) -> Optional[Dict[str, Any]]:
        try:
            resp = self._request("GET", f"/api/channel/{channel_id}")
        except NewApiError:
            return None
        if resp.get("success") is not True:
            return None
        return resp.get("data")

    def add_channel(self, channel: Dict[str, Any]) -> None:
        resp = self._request("POST", "/api/channel/", {"mode": "single", "channel": channel})
        self._ok(resp, "/api/channel/")

    def update_channel(self, channel: Dict[str, Any]) -> None:
        # rc.49 明确拒绝 update 携带 status 字段
        channel = {k: v for k, v in channel.items() if k != "status"}
        resp = self._request("PUT", "/api/channel/", channel)
        self._ok(resp, "/api/channel/")

    # ---------- 用户（AdminAuth） ----------
    def list_users(self) -> List[Dict[str, Any]]:
        return self._list_all("/api/user/?", "items")

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        try:
            resp = self._request("GET", f"/api/user/{user_id}")
        except NewApiError:
            return None
        if resp.get("success") is not True:
            return None
        return resp.get("data")

    # ---------- 充值（AdminAuth） ----------
    def list_topups(self) -> List[Dict[str, Any]]:
        return self._list_all("/api/user/topup?", "items")

    # ---------- 日志（AdminAuth） ----------
    def list_logs(
        self,
        log_type: int = 0,
        start_timestamp: int = 0,
        end_timestamp: int = 0,
    ) -> List[Dict[str, Any]]:
        q = urllib.parse.urlencode(
            {"type": log_type, "start_timestamp": start_timestamp, "end_timestamp": end_timestamp}
        )
        return self._list_all(f"/api/log/?{q}", "items")

    # ---------- 令牌（UserAuth，acceptance 用） ----------
    def create_token(self, name: str, *, unlimited_quota: bool = True) -> None:
        payload: Dict[str, Any] = {
            "name": name,
            "unlimited_quota": unlimited_quota,
            "remain_quota": 0,
            "expired_time": -1,
            "group": "default",
            "model_limits_enabled": False,
        }
        resp = self._request("POST", "/api/token/", payload)
        self._ok(resp, "/api/token/")

    def list_tokens(self) -> List[Dict[str, Any]]:
        return self._list_all("/api/token/?", "items")

    def get_token_key(self, token_id: int) -> str:
        resp = self._request("POST", f"/api/token/{token_id}/key", {})
        self._ok(resp, f"/api/token/{token_id}/key")
        data = resp.get("data") or {}
        if isinstance(data, dict):
            return str(data.get("key", "") or "")
        return str(data or "")

    # ---------- 下单（UserAuth，acceptance 用） ----------
    def request_epay(self, amount: int, payment_method: str) -> Dict[str, Any]:
        """创建易支付/加密充值订单。返回 {message, data(params), url}。"""
        resp = self._request(
            "POST",
            "/api/user/pay",
            {"amount": amount, "payment_method": payment_method},
        )
        if resp.get("message") != "success":
            raise NewApiError(
                f"/api/user/pay 失败: {str(resp.get('data', resp.get('message', '')))[:500]}"
            )
        return resp


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
