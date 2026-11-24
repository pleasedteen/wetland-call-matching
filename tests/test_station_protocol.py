"""端到端协议测试：限流等待、会话轮换恢复、回执链断裂拒绝。

为保证测试快速确定，限流窗口与轮换阈值在测试内 monkeypatch，
每个用例使用独立的一次性站点服务。
"""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from birdsurvey import station as station_mod  # noqa: E402
from birdsurvey import dataset  # noqa: E402
from birdsurvey.client import StationClient  # noqa: E402
from birdsurvey.station import make_server  # noqa: E402


def http_json(url, method="GET", headers=None):
    req = urllib.request.Request(url, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class ServerHarness:
    """启动一座打了补丁的站点，with 退出时关闭并还原常量。"""

    def __init__(self, **patches):
        self.patches = patches
        self.saved = {}

    def __enter__(self):
        for k, v in self.patches.items():
            self.saved[k] = getattr(station_mod, k)
            setattr(station_mod, k, v)
        self.server = make_server("A", "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        for k, v in self.saved.items():
            setattr(station_mod, k, v)
        return False


class TestStationProtocol(unittest.TestCase):
    def test_rate_limit_returns_retry_after(self):
        # 新会话直接翻页：窗口内 2 次配额，第 3 次页面请求必然 429
        with ServerHarness(RATE_LIMIT=2, RATE_WINDOW=2.0) as base:
            client = StationClient(base)
            sid = client.new_session()
            periods = client.list_periods()
            cursor = periods[0]["first_cursor"]
            receipt = None
            saw_429 = False
            retry_after = None
            for _ in range(4):
                headers = {"X-Session-Id": sid}
                if receipt:
                    headers["X-Page-Receipt"] = receipt
                status, body = http_json(
                    f"{base}/periods/0/pages?cursor={cursor}", headers=headers
                )
                if status == 429:
                    saw_429 = True
                    retry_after = body["error"]["retry_after"]
                    break
                self.assertEqual(status, 200, body)
                receipt = body["page"]["receipt"]
                cursor = body["page"]["next_cursor"]
            self.assertTrue(saw_429)
            self.assertGreater(retry_after, 0)
            self.assertLessEqual(retry_after, 2.0)

    def test_client_waits_and_recovers_from_rate_limit(self):
        # 客户端视角：限流后按 retry_after 等待，同一页重试成功
        with ServerHarness(RATE_LIMIT=2, RATE_WINDOW=0.3) as base:
            client = StationClient(base)
            periods = {p["period"]: p for p in client.list_periods()}
            pages = list(client.fetch_period(0, periods[0]["first_cursor"]))
            total_pages = pages[0]["total_pages"]
            # 重取可能产生重复页事件，去重后页号应覆盖全部页
            page_nos = sorted({ev["page"] for ev in pages})
            self.assertEqual(page_nos, list(range(total_pages)))

    def test_client_survives_session_rotation_and_dedups(self):
        # 建会话计第 1 次；预加载 3 次 list_periods（计数到 4），
        # 再 list_periods 拿游标（第 5 次，计数到 5），随后 2 次成功取页
        # （计数到 7 即第 7 次请求）触发 409；客户端新建会话从首页恢复，
        # 新会话预算充足，最终读全 3 页并产出 restarted 事件。
        with ServerHarness(ROTATE_AFTER=7, RATE_LIMIT=10_000, RATE_WINDOW=0.0) as base:
            client = StationClient(base)
            client.new_session()
            for _ in range(3):
                client.list_periods()
            periods = {p["period"]: p for p in client.list_periods()}
            pages = list(client.fetch_period(0, periods[0]["first_cursor"]))
            page_nos = sorted({ev["page"] for ev in pages})
            self.assertEqual(page_nos, list(range(pages[0]["total_pages"])))
            self.assertTrue(any(ev["restarted"] for ev in pages))
            ids = {rid for ev in pages for rid in (r["id"] for r in ev["records"])}
            truth = {
                r["id"] for r in dataset.build_period("A", 0) if not r.get("_corrupt")
            }
            self.assertEqual(ids, truth)

    def test_missing_receipt_rejected(self):
        with ServerHarness(RATE_LIMIT=10_000) as base:
            client = StationClient(base)
            sid = client.new_session()
            periods = client.list_periods()
            first = periods[0]["first_cursor"]
            status, body = http_json(
                f"{base}/periods/0/pages?cursor={first}",
                headers={"X-Session-Id": sid},
            )
            self.assertEqual(status, 200)
            next_cursor = body["page"]["next_cursor"]
            status, body = http_json(
                f"{base}/periods/0/pages?cursor={next_cursor}",
                headers={"X-Session-Id": sid},
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "bad_receipt")

    def test_old_cursor_invalid_after_rotation(self):
        with ServerHarness(ROTATE_AFTER=3, RATE_LIMIT=10_000) as base:
            client = StationClient(base)
            sid = client.new_session()            # 计数 1
            periods = client.list_periods()        # 计数 2（成功）
            cursor = periods[0]["first_cursor"]
            # 第 3 次请求触发轮换：该页请求本身即 409
            status, body = http_json(
                f"{base}/periods/0/pages?cursor={cursor}",
                headers={"X-Session-Id": sid},
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "session_rotated")
            # 旧会话后续请求一律 409，旧翻页令牌随之失效
            status, body = http_json(
                f"{base}/periods", headers={"X-Session-Id": sid}
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "session_rotated")


if __name__ == "__main__":
    unittest.main()
