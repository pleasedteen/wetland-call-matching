"""单站模拟录音站 HTTP 服务。

协议要点（两站行为一致，数据不同）：
- POST /sessions        建立会话，返回 session_id；会话轮换后旧会话立即失效
- GET  /periods         列出全部已封存时段（含首页游标）
- GET  /periods/{p}/pages?cursor=<token>
      分页取数；第 2 页起必须带 X-Page-Receipt（上一页回执）
- 服务端行为：
  * 每个会话满 ROTATE_AFTER 次请求即轮换，旧会话/旧游标全部失效 -> 409
  * 站点级限流 -> 429 + retry_after，调用方按秒等待
  * 页码与回执不匹配 -> 400
  * 页面在 table / card 两种版式间交替，损坏条目以残缺结构下发
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import dataset

ROTATE_AFTER = 11          # 每个会话的请求上限，随后轮换
RATE_WINDOW = 6.0
RATE_LIMIT = 7             # 窗口内允许的页面请求数
RECEIPT_REQUIRED_FROM_PAGE = 1


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


class StationState:
    """单站全部可变状态（线程安全）。"""

    def __init__(self, station: str):
        if station not in dataset.STATIONS:
            raise ValueError(f"未知站点: {station}")
        self.station = station
        self.data = dataset.build_station(station)
        self._lock = threading.Lock()
        self.sessions: dict[str, bytes] = {}
        self._request_counts: dict[str, int] = {}
        self.page_hits: list[float] = []

    # -- 会话 -------------------------------------------------------------
    def create_session(self) -> str:
        sid = _b64e(hashlib.sha1(f"{self.station}-{time.time_ns()}".encode()).digest())[:20]
        with self._lock:
            self.sessions[sid] = hashlib.sha256(sid.encode()).digest()
            # 建会话本身计为第 1 次请求，后续还允许 ROTATE_AFTER-1 次成功请求
            self._request_counts[sid] = 1
        return sid

    def _key_of(self, sid: str) -> bytes | None:
        return self.sessions.get(sid)

    # -- 签名令牌 / 回执 ---------------------------------------------------
    def sign(self, sid: str, payload: dict) -> str:
        key = self._key_of(sid)
        if key is None:
            raise KeyError(sid)
        body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        mac = hmac.new(key, body.encode(), hashlib.sha256).digest()
        return f"{body}.{_b64e(mac)}"

    def verify(self, sid: str, token: str) -> dict:
        key = self._key_of(sid)
        if key is None or "." not in token:
            raise PermissionError("session_rotated")
        body, mac = token.split(".", 1)
        expect = hmac.new(key, body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64e(expect), mac):
            raise PermissionError("bad_signature")
        return json.loads(_b64d(body))

    # -- 请求计数 / 轮换 / 限流 -------------------------------------------
    def touch(self, sid: str) -> None:
        """记录一次会话请求；达到轮换阈值的当次请求即作废旧会话。

        语义：会话有效期为 ROTATE_AFTER-1 次成功请求，第 ROTATE_AFTER 次
        请求直接 409，调用方必须新建会话；旧游标随旧会话一并失效。
        """
        with self._lock:
            if sid not in self.sessions:
                raise PermissionError("session_rotated")
            self._request_counts[sid] += 1
            if self._request_counts[sid] >= ROTATE_AFTER:
                del self.sessions[sid]
                del self._request_counts[sid]
                raise PermissionError("session_rotated")

    def rate_gate(self) -> float:
        """返回需要等待的秒数；0 表示放行。"""
        now = time.monotonic()
        with self._lock:
            self.page_hits = [t for t in self.page_hits if now - t < RATE_WINDOW]
            if len(self.page_hits) >= RATE_LIMIT:
                return RATE_WINDOW - (now - self.page_hits[0])
            self.page_hits.append(now)
            return 0.0

    # -- 分页 -------------------------------------------------------------
    def page_payload(self, period: int, page: int) -> dict:
        records = self.data[period]
        start = page * dataset.PAGE_SIZE
        chunk = records[start : start + dataset.PAGE_SIZE]
        total_pages = (len(records) + dataset.PAGE_SIZE - 1) // dataset.PAGE_SIZE
        layout = "table" if page % 2 == 0 else "card"
        items: list[dict] = []
        for r in chunk:
            if r.get("_corrupt"):
                if layout == "table":
                    # 残缺行：缺少 id 与 species，解析器须跳过
                    items.append({"ts": r["ts"], "freq_hz": r["freq_hz"]})
                else:
                    items.append({"damaged": True, "raw": "???"})
                continue
            if layout == "table":
                items.append(
                    {
                        "id": r["id"],
                        "ts": r["ts"],
                        "species": r["species"],
                        "freq_hz": r["freq_hz"],
                        "confidence": r["confidence"],
                    }
                )
            else:
                items.append(
                    {
                        "card_id": r["id"],
                        "when": r["ts"],
                        "bird": r["species"],
                        "freq_hz": r["freq_hz"],
                        "confidence": r["confidence"],
                    }
                )
        return {
            "station": self.station,
            "period": period,
            "page": page,
            "total_pages": total_pages,
            "layout": layout,
            "items": items,
        }


class StationHandler(BaseHTTPRequestHandler):
    server_version = "BirdStation/1.0"

    @property
    def state(self) -> StationState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # 静音常规访问日志
        return

    # -- 工具 -------------------------------------------------------------
    def send_json(self, status: int, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def error(self, status: int, code: str, message: str, retry_after: float | None = None) -> None:
        err: dict = {"code": code, "message": message}
        if retry_after is not None:
            err["retry_after"] = round(retry_after, 3)
        self.send_json(status, {"ok": False, "error": err})

    def session_id(self) -> str | None:
        return self.headers.get("X-Session-Id")

    # -- 路由 -------------------------------------------------------------
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/sessions":
            sid = self.state.create_session()
            self.send_json(200, {"ok": True, "session_id": sid})
            return
        self.error(404, "not_found", f"未知路径 {path}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path == "/periods":
                self._handle_periods()
            elif path.startswith("/periods/") and path.endswith("/pages"):
                self._handle_page(path, qs)
            elif path == "/health":
                self.send_json(200, {"ok": True, "station": self.state.station})
            else:
                self.error(404, "not_found", f"未知路径 {path}")
        except PermissionError as exc:
            # 会话已轮换或签名失效：旧游标一律不可继续
            self.error(409, "session_rotated", f"会话失效，请新建会话后重取: {exc}")
        except KeyError as exc:
            self.error(400, "bad_cursor", f"游标无效: {exc}")
        except ValueError as exc:
            self.error(400, "bad_request", str(exc))

    # -- 处理器 -----------------------------------------------------------
    def _handle_periods(self) -> None:
        sid = self.session_id()
        if not sid:
            self.error(401, "no_session", "缺少 X-Session-Id")
            return
        self.state.touch(sid)
        periods = []
        for p in sorted(self.state.data):
            start, end = dataset.period_window(p)
            periods.append(
                {
                    "period": p,
                    "start_ts": start,
                    "end_ts": end,
                    "entry_count": len(self.state.data[p]),
                    "first_cursor": self.state.sign(sid, {"sid": sid, "p": p, "n": 0}),
                }
            )
        self.send_json(200, {"ok": True, "periods": periods})

    def _handle_page(self, path: str, qs: dict) -> None:
        sid = self.session_id()
        if not sid:
            self.error(401, "no_session", "缺少 X-Session-Id")
            return
        try:
            period = int(path.split("/")[2])
        except (IndexError, ValueError):
            self.error(400, "bad_request", "时段路径格式错误")
            return
        if period not in self.state.data:
            self.error(404, "unknown_period", f"不存在的时段 {period}")
            return
        cursor = qs.get("cursor", [""])[0]
        if not cursor:
            self.error(400, "bad_cursor", "缺少 cursor 参数")
            return

        self.state.touch(sid)  # 可能触发轮换 -> 409
        payload = self.state.verify(sid, cursor)  # 轮换后 -> 409
        if payload.get("p") != period or payload.get("sid") != sid:
            raise KeyError("游标与时段/会话不匹配")
        page = int(payload["n"])
        total_pages = (
            len(self.state.data[period]) + dataset.PAGE_SIZE - 1
        ) // dataset.PAGE_SIZE
        if page >= total_pages:
            raise KeyError("页码越界")

        if page >= RECEIPT_REQUIRED_FROM_PAGE:
            receipt = self.headers.get("X-Page-Receipt", "")
            if not receipt:
                self.error(400, "bad_receipt", "缺少上一页回执 X-Page-Receipt")
                return
            prev = self.state.verify(sid, receipt)
            if (
                prev.get("p") != period
                or prev.get("sid") != sid
                or prev.get("n") != page - 1
            ):
                self.error(400, "bad_receipt", "回执与请求页不匹配，翻页链断裂")
                return

        wait = self.state.rate_gate()
        if wait > 0:
            self.error(429, "rate_limited", "站点临时限流", retry_after=wait)
            return

        body = self.state.page_payload(period, page)
        body["receipt"] = self.state.sign(
            sid,
            {"sid": sid, "p": period, "n": page, "count": len(body["items"])},
        )
        body["next_cursor"] = (
            self.state.sign(sid, {"sid": sid, "p": period, "n": page + 1})
            if page + 1 < total_pages
            else None
        )
        self.send_json(200, {"ok": True, "page": body})


def make_server(station: str, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), StationHandler)
    server.state = StationState(station)  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="启动一座模拟鸟鸣录音站")
    ap.add_argument("--station", required=True, choices=list(dataset.STATIONS))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args(argv)

    server = make_server(args.station, args.host, args.port)
    print(f"station {args.station} listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
