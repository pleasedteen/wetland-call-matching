"""录音站 HTTP 客户端。

职责：
- 会话建立与自动恢复（409 后新建会话、重列时段、从第 1 页重取）；
- 限流处理（429 按 retry_after 等待后重试同一请求）；
- table / card 两种版式解析为统一记录，损坏条目跳过并计数；
- 翻页回执链（X-Page-Receipt）由客户端自动维护。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Iterator


class StationError(RuntimeError):
    """站点返回的业务错误基类。"""


class SessionRotatedError(StationError):
    """旧会话/游标已失效（409）。"""


class RateLimitedError(StationError):
    """站点限流（429），retry_after 为建议等待秒数。"""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


class BadRequestError(StationError):
    """请求/回执/游标被站点拒绝（400/401/404）。"""


REQUIRED_FIELDS = ("id", "ts", "species")


def parse_items(items: list[dict], layout: str) -> tuple[list[dict], int]:
    """把一页原始条目解析为统一结构，返回 (可读记录, 损坏数)。

    损坏判定：缺字段、字段类型非法、值越界等一律跳过，
    同页其余可读记录必须保留。
    """
    out: list[dict] = []
    bad = 0
    if layout not in ("table", "card"):
        raise ValueError(f"未知版式 {layout}")
    for item in items:
        try:
            if layout == "table":
                rec = {
                    "id": item["id"],
                    "ts": float(item["ts"]),
                    "species": item["species"],
                    "freq_hz": item.get("freq_hz"),
                    "confidence": item.get("confidence"),
                }
            elif layout == "card":
                rec = {
                    "id": item["card_id"],
                    "ts": float(item["when"]),
                    "species": item["bird"],
                    "freq_hz": item.get("freq_hz"),
                    "confidence": item.get("confidence"),
                }
            if not all(rec[k] is not None for k in REQUIRED_FIELDS):
                raise ValueError("必填字段为空")
            if not isinstance(rec["id"], str) or not rec["id"]:
                raise ValueError("id 非法")
            if not isinstance(rec["species"], str) or not rec["species"]:
                raise ValueError("species 非法")
        except (KeyError, TypeError, ValueError):
            bad += 1
            continue
        out.append(rec)
    return out, bad


class StationClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session_id: str | None = None

    # -- 底层 HTTP --------------------------------------------------------
    def _request(
        self, method: str, path: str, headers: dict | None = None
    ) -> dict:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode())
            except (ValueError, UnicodeDecodeError):
                body = {}
            err = body.get("error", {}) if isinstance(body, dict) else {}
            code = err.get("code", f"http_{exc.code}")
            if exc.code == 409:
                raise SessionRotatedError(code)
            if exc.code == 429:
                raise RateLimitedError(code, float(err.get("retry_after", 1.0)))
            raise BadRequestError(f"{code}: {err.get('message', '')}") from exc
        except urllib.error.URLError as exc:
            raise StationError(f"连接失败 {url}: {exc}") from exc

    def new_session(self) -> str:
        body = self._request("POST", "/sessions")
        self.session_id = body["session_id"]
        return self.session_id

    def list_periods(self) -> list[dict]:
        if self.session_id is None:
            self.new_session()
        body = self._request(
            "GET", "/periods", {"X-Session-Id": self.session_id}
        )
        return body["periods"]

    def _fetch_page(self, period: int, cursor: str, receipt: str | None) -> dict:
        if self.session_id is None:
            self.new_session()
        headers = {"X-Session-Id": self.session_id}
        if receipt is not None:
            headers["X-Page-Receipt"] = receipt
        body = self._request(
            "GET", f"/periods/{period}/pages?cursor={cursor}", headers
        )
        return body["page"]

    # -- 高层：整个时段翻页（自动限流等待 + 会话恢复） --------------------
    def fetch_period(
        self,
        period: int,
        first_cursor: str,
        max_attempts: int = 50,
    ) -> Iterator[dict]:
        """逐页产出事件字典，会话轮换时自动从首页重取。

        产出：{"page", "total_pages", "layout", "records", "bad",
               "restarted": bool}
        重取产生的重复记录由采集端按 id 去重。
        """
        if self.session_id is None:
            self.new_session()
        attempts = 0
        restarted = False
        while attempts < max_attempts:
            attempts += 1
            cursor: str | None = first_cursor
            receipt: str | None = None
            while cursor is not None:
                try:
                    page = self._fetch_page(period, cursor, receipt)
                except RateLimitedError as exc:
                    time.sleep(max(0.0, exc.retry_after))
                    continue  # 同一页重试，游标/回执不变
                except SessionRotatedError:
                    # 会话轮换：旧令牌全部失效，按新会话恢复
                    self.new_session()
                    fresh = {p["period"]: p for p in self.list_periods()}
                    first_cursor = fresh[period]["first_cursor"]
                    restarted = True
                    break  # 退出内层循环，从第 1 页重新翻
                records, bad = parse_items(page["items"], page["layout"])
                yield {
                    "page": page["page"],
                    "total_pages": page["total_pages"],
                    "layout": page["layout"],
                    "records": records,
                    "bad": bad,
                    "restarted": restarted,
                }
                receipt = page["receipt"]
                cursor = page["next_cursor"]
            else:
                return  # 正常翻到末页
        raise StationError(f"时段 {period} 重取超过 {max_attempts} 次仍失败")
