"""分组明细导出与快照归档的 HTTP 级集成测试。

不引入 httpx/TestClient：使用标准库 urllib 驱动内嵌 uvicorn 服务，
验证路由顺序、错误状态码、下载响应头、并发幂等等真实网络行为。
"""
from __future__ import annotations

import copy
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uvicorn  # noqa: E402

from app.main import app  # noqa: E402
from app.store import store  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class ApiClient:
    def __init__(self, base: str) -> None:
        self.base = base

    @staticmethod
    def _header(headers: dict, name: str) -> str:
        wanted = name.lower()
        for key, value in headers.items():
            if key.lower() == wanted:
                return value
        return ""

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict, object]:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read()
                headers = dict(resp.headers.items())
                return resp.status, headers, json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                parsed = json.loads(body) if body else None
            except json.JSONDecodeError:
                parsed = body.decode()
            return exc.code, dict(exc.headers.items()), parsed


class DrillExportArchiveApiTest(unittest.TestCase):
    server: uvicorn.Server
    server_thread: threading.Thread
    api: ApiClient

    @classmethod
    def setUpClass(cls) -> None:
        port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        cls.server = uvicorn.Server(config)
        cls.server_thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.server_thread.start()
        cls.api = ApiClient(f"http://127.0.0.1:{port}")
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                status, _, _ = cls.api.request("GET", "/api/health")
                if status == 200:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("测试服务器未能在时限内启动")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.server_thread.join(timeout=5)

    def setUp(self) -> None:
        store._artifacts.clear()
        store._compute_cache.clear()
        store._snapshots.clear()
        store._reset_export_runtime()

    def _prepare_export(self) -> dict:
        status, _, _ = self.api.request("POST", "/api/tag-drill/TD-001/run")
        self.assertEqual(status, 200)
        status, _, body = self.api.request("POST", "/api/tag-drill/TD-001/prepare-export", {})
        self.assertEqual(status, 200)
        return body["entry"]

    def test_full_lifecycle_export_archive_download_trace(self) -> None:
        export_file = self._prepare_export()
        self.assertIn("parent_tag", export_file)
        self.assertEqual(export_file["parent_tag"]["version"], "v1.0")
        self.assertIn("source_result_ids", export_file)

        # 静态路由 /exports 不被 /{entry_id} 吞掉
        status, _, body = self.api.request("GET", "/api/tag-drill/exports")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 1)

        status, headers, body = self.api.request(
            "GET", f"/api/tag-drill/exports/{export_file['export_id']}"
        )
        self.assertEqual(status, 200)
        self.assertIn("attachment", self.api._header(headers, "Content-Disposition"))
        self.assertEqual(body["export_id"], export_file["export_id"])

        # 归档确认
        status, _, body = self.api.request(
            "POST",
            "/api/tag-snapshot/archives",
            {"archive_id": "ARC-HTTP-1", "export_file": export_file, "snapshot_id": "SN-001"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["entry"]["status"], "confirmed")

        # 下载：同一归档编号
        status, headers, body = self.api.request(
            "GET", "/api/tag-snapshot/archives/ARC-HTTP-1/download"
        )
        self.assertEqual(status, 200)
        self.assertIn("ARC-HTTP-1", self.api._header(headers, "Content-Disposition"))
        self.assertEqual(
            body["export_file"]["snapshot_ref"]["snapshot_id"],
            "SN-001",
        )

        # 来源回查
        status, _, trace = self.api.request(
            "GET", "/api/tag-snapshot/archives/ARC-HTTP-1/trace"
        )
        self.assertEqual(status, 200)
        self.assertTrue(trace["all_verified"])
        self.assertEqual(trace["verified_groups"], trace["total_groups"])

    def test_failure_then_retry_then_replay_is_idempotent(self) -> None:
        export_file = self._prepare_export()

        # 写入后失败
        status, _, body = self.api.request(
            "POST",
            "/api/tag-snapshot/archives",
            {
                "archive_id": "ARC-HTTP-2",
                "export_file": export_file,
                "simulate_failure": "after_write",
            },
        )
        self.assertEqual(status, 500)
        self.assertEqual(body["detail"]["code"], "archive_write_failed")

        # 未确认：下载 409、回查 409
        status, _, body = self.api.request(
            "GET", "/api/tag-snapshot/archives/ARC-HTTP-2/download"
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["detail"]["code"], "attachment_not_confirmed")
        status, _, body = self.api.request(
            "GET", "/api/tag-snapshot/archives/ARC-HTTP-2/trace"
        )
        self.assertEqual(status, 409)

        # 状态查询可见中断记录
        status, _, record = self.api.request(
            "GET", "/api/tag-snapshot/archives/ARC-HTTP-2"
        )
        self.assertEqual(status, 200)
        self.assertEqual(record["status"], "archiving")

        # 原请求补交
        status, _, body = self.api.request(
            "POST",
            "/api/tag-snapshot/archives",
            {"archive_id": "ARC-HTTP-2", "export_file": export_file},
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["entry"]["idempotent_replay"])

        # 再补交：幂等回放原文件
        status, _, body = self.api.request(
            "POST",
            "/api/tag-snapshot/archives",
            {"archive_id": "ARC-HTTP-2", "export_file": export_file},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["entry"]["idempotent_replay"])
        self.assertEqual(body["entry"]["export_id"], export_file["export_id"])

        status, _, listing = self.api.request("GET", "/api/tag-snapshot/archives")
        self.assertEqual(
            len([r for r in listing["items"] if r["archive_id"] == "ARC-HTTP-2"]),
            1,
        )

    def test_incomplete_attachment_returns_422(self) -> None:
        export_file = self._prepare_export()
        broken = copy.deepcopy(export_file)
        del broken["hierarchy_snapshot"]
        status, _, body = self.api.request(
            "POST",
            "/api/tag-snapshot/archives",
            {"archive_id": "ARC-HTTP-BAD", "export_file": broken},
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["detail"]["code"], "incomplete_attachment")

    def test_unknown_archive_returns_404(self) -> None:
        status, _, body = self.api.request(
            "GET", "/api/tag-snapshot/archives/NOPE/download"
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["detail"]["code"], "archive_not_found")

    def test_hierarchy_conflict_flow_over_http(self) -> None:
        export_file = self._prepare_export()

        status, _, body = self.api.request(
            "POST",
            "/api/tag-drill/move-tag",
            {"child_tag": "TAG-002", "new_parent": "TAG-003"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["entry"]["old_parent"], "TAG-001")

        # 中断后补交
        self.api.request(
            "POST",
            "/api/tag-snapshot/archives",
            {
                "archive_id": "ARC-HTTP-H",
                "export_file": export_file,
                "simulate_failure": "before_confirm",
            },
        )
        status, _, body = self.api.request(
            "POST",
            "/api/tag-snapshot/archives",
            {"archive_id": "ARC-HTTP-H", "export_file": export_file},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["entry"]["hierarchy_conflict"])

        status, _, downloaded = self.api.request(
            "GET", "/api/tag-snapshot/archives/ARC-HTTP-H/download"
        )
        archived = downloaded["export_file"]
        # 旧层级仍在文件里
        self.assertEqual(
            sorted(archived["hierarchy_snapshot"]["TAG-001"]),
            ["TAG-002", "TAG-003"],
        )
        self.assertEqual(archived["conflict_detail"]["current_child_tags"], ["TAG-003"])
        # 统计值与来源回查一致
        status, _, trace = self.api.request(
            "GET", "/api/tag-snapshot/archives/ARC-HTTP-H/trace"
        )
        self.assertTrue(trace["all_verified"])
        self.assertTrue(trace["hierarchy_conflict"])

    def test_concurrent_archive_requests_single_delivery(self) -> None:
        export_file = self._prepare_export()

        # 先确定性地制造一次“写入后、确认前”中断
        status, _, body = self.api.request(
            "POST",
            "/api/tag-snapshot/archives",
            {
                "archive_id": "ARC-HTTP-C",
                "export_file": export_file,
                "simulate_failure": "after_write",
            },
        )
        self.assertEqual(status, 500)
        self.assertEqual(body["detail"]["code"], "archive_write_failed")

        # 运营并发用原归档请求补交
        outcomes: list[tuple[int, object]] = []
        barrier = threading.Barrier(6)

        def worker() -> None:
            barrier.wait()
            status, _, body = self.api.request(
                "POST",
                "/api/tag-snapshot/archives",
                {"archive_id": "ARC-HTTP-C", "export_file": copy.deepcopy(export_file)},
            )
            outcomes.append((status, body))

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertTrue(all(status == 200 for status, _ in outcomes))
        confirmed = [
            o for o in outcomes
            if not o[1]["entry"].get("idempotent_replay")
        ]
        replays = [
            o for o in outcomes
            if o[1]["entry"].get("idempotent_replay")
        ]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(len(replays), 5)

        status, _, record = self.api.request(
            "GET", "/api/tag-snapshot/archives/ARC-HTTP-C"
        )
        self.assertEqual(status, 200)
        self.assertEqual(record["status"], "confirmed")
        self.assertEqual(record["delivery_count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
