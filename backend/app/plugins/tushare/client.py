"""Tushare 开放数据接口 HTTP 客户端。

职责: Token 认证、单一端点 POST、列式响应解包。不知道 provider / services 层。
文档: https://tushare.pro/document/2 — 单一端点 POST http://api.tushare.pro,
请求体 {"api_name","token","params","fields"}, 响应
{"code","msg","data":{"fields":[...],"items":[[...]]}}(列式, 需 zip 成行)。

注意: 官方端点仅提供 http(https 不可用); code == 0 为成功, 其余为业务错误
(如 50101 缺少必填参数 / 40203 频率超限), 统一抛 TushareError。
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "http://api.tushare.pro"
DEFAULT_TIMEOUT = 30.0


class TushareError(Exception):
    """Tushare 接口错误(配置缺失 / 网络失败 / 业务 code != 0)。"""


class TushareClient:
    """Tushare REST 客户端 (线程安全: httpx.Client 可并发复用)。"""

    def __init__(
        self, token: str, base_url: str = BASE_URL, timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        if not token:
            raise TushareError("未配置 TUSHARE_API_KEY")
        self._token = token
        self._base_url = base_url
        self._http = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def post(
        self, api_name: str, params: dict | None = None, fields: str = ""
    ) -> list[dict]:
        """调用单个 Tushare 接口, 列式响应解包为行 dict 列表。

        fields 为逗号分隔的字段白名单(留空取上游默认字段集), 用于裁剪无用列
        或获取不在默认集里的字段(如 stock_basic.exchange)。
        """
        body = {
            "api_name": api_name,
            "token": self._token,
            "params": params or {},
            "fields": fields or "",
        }
        try:
            resp = self._http.post(self._base_url, json=body)
        except httpx.HTTPError as e:
            raise TushareError(f"网络请求失败: {e}") from e
        if resp.status_code != 200:
            raise TushareError(f"HTTP {resp.status_code}: {api_name}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise TushareError(f"响应不是 JSON: {api_name}") from e
        code = payload.get("code")
        if code not in (0, "0", None):
            raise TushareError(
                f"Tushare 接口错误 code={code}: {payload.get('msg', '')} ({api_name})"
            )
        data = payload.get("data") or {}
        names = data.get("fields") or []
        items = data.get("items") or []
        if not names:
            return []
        return [dict(zip(names, item, strict=False)) for item in items]
