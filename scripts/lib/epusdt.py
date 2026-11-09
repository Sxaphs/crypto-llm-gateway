"""epusdt (GMWalletApp/epusdt) 官方管理 API 客户端。

路由/鉴权/字段已对照 epusdt 源码核实：
- 登录:     POST /admin/api/v1/auth/login            {username,password} -> data.token (JWT)
- 列表:     GET  /admin/api/v1/orders?keyword=&status=&start_at=&end_at=&page=&page_size=
            -> data.list[] / data.total
- 单查:     GET  /admin/api/v1/orders/{trade_id}     -> data
- 重放回调: POST /admin/api/v1/orders/{trade_id}/resend-callback
            （仅 status=2 支付成功且 notify_url 非空时可用；把 callback_confirm 重置为未回调，由 mq 重投）
- 鉴权:    Authorization: Bearer <jwt>
- 成功封装: HTTP 200 + {"status_code":200,"message":"success","data":...}
订单字段(JSON): trade_id, order_id, block_transaction_id, amount(CNY), currency,
    actual_amount(加密数量), receive_address, token, network, status(1待付/2成功/3过期/4待选),
    notify_url, redirect_url, name, callback_num, callback_confirm(1成功/2未回调), pay_provider ...

密钥绝不打印。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


class EpusdtError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class EpusdtAdminClient:
    def __init__(self, base_url: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token: str = ""

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        auth: bool = True,
    ) -> Dict[str, Any]:
        url = self.base_url + path
        body = None
        headers = {"Accept": "application/json"}
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth and self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
            raise EpusdtError(
                f"HTTP {exc.code} {method} {path}: {raw[:300]}", exc.code
            ) from exc
        except urllib.error.URLError as exc:
            raise EpusdtError(f"连接失败 {method} {path}: {exc.reason}") from exc

        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise EpusdtError(f"响应不是 JSON {method} {path}") from exc

    def _ok(self, resp: Dict[str, Any], path: str) -> Dict[str, Any]:
        if resp.get("status_code") == 200:
            return resp
        raise EpusdtError(
            f"{path} 失败: status_code={resp.get('status_code')} {str(resp.get('message',''))[:300]}",
            resp.get("status_code"),
        )

    def login(self, username: str, password: str) -> str:
        resp = self._request(
            "POST",
            "/admin/api/v1/auth/login",
            {"username": username, "password": password},
            auth=False,
        )
        self._ok(resp, "/admin/api/v1/auth/login")
        token = (resp.get("data") or {}).get("token", "")
        if not token:
            raise EpusdtError("登录未返回 token")
        self.token = token
        return token

    def list_orders(
        self,
        keyword: str = "",
        status: int = 0,
        start_at: int = 0,
        end_at: int = 0,
        page: int = 1,
        page_size: int = 200,
    ) -> Dict[str, Any]:
        params = {"page": str(page), "page_size": str(page_size)}
        if keyword:
            params["keyword"] = keyword
        if status:
            params["status"] = str(status)
        if start_at:
            params["start_at"] = str(start_at)
        if end_at:
            params["end_at"] = str(end_at)
        qs = urllib.parse.urlencode(params)
        resp = self._request("GET", f"/admin/api/v1/orders?{qs}")
        self._ok(resp, "/admin/api/v1/orders")
        data = resp.get("data") or {}
        return {
            "list": data.get("list") or [],
            "total": int(data.get("total") or 0),
        }

    def get_order(self, trade_id: str) -> Optional[Dict[str, Any]]:
        resp = self._request("GET", f"/admin/api/v1/orders/{trade_id}")
        if resp.get("status_code") != 200:
            return None
        return resp.get("data")

    def resend_callback(self, trade_id: str) -> None:
        resp = self._request("POST", f"/admin/api/v1/orders/{trade_id}/resend-callback")
        self._ok(resp, "/admin/api/v1/orders/{trade_id}/resend-callback")

    def list_wallets(self) -> List[Dict[str, Any]]:
        """返回启用中的钱包地址列表（含 network/address）。"""
        resp = self._request("GET", "/admin/api/v1/wallets")
        self._ok(resp, "/admin/api/v1/wallets")
        data = resp.get("data") or []
        if not isinstance(data, list):
            return []
        out = []
        for w in data:
            if not isinstance(w, dict):
                continue
            if int(w.get("status", 1) or 1) == 1:
                out.append(
                    {
                        "network": str(w.get("network", "") or ""),
                        "address": str(w.get("address", "") or ""),
                    }
                )
        return out
