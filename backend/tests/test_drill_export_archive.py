"""标签下钻分组明细导出与标签快照归档的服务层验收测试。

覆盖：
1. 导出文件来自同一次下钻，冻结父子标签版本/分组口径/统计值/原始结果编号/层级；
2. 归档幂等：同一归档编号并发与串行重试只交付一份快照，确认后返回原文件；
3. 附件不完整不可归档、未确认不可下载/回查；
4. 写入后失败与确认前中断：实际写入已完成时，原请求重试恢复且只交付一次；
5. 子标签换父级后，文件保留旧层级，冲突被标记，统计值与快照引用仍一致可回查。
"""
from __future__ import annotations

import copy
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import store
from app.services.tag_compute import compute_tag
from app.services.tag_drill import drill_tag, prepare_export, move_child_tag
from app.services.tag_snapshot import (
    ArchiveError,
    download_archive_file,
    get_archive_record,
    list_archive_records,
    submit_archive,
    trace_archive_sources,
)


class DrillExportArchiveTest(unittest.TestCase):
    def setUp(self) -> None:
        store._artifacts.clear()
        store._compute_cache.clear()
        store._snapshots.clear()
        store._reset_export_runtime()

    # ---------- 导出文件准备 ----------
    def test_export_file_freezes_versions_basis_values_and_source_ids(self) -> None:
        compute_tag("TC-001")
        run = drill_tag("TD-001")
        export_file = prepare_export("TD-001")

        # 一份文件来自同一次下钻
        self.assertEqual(export_file["drill_run_id"], run["drill_run_id"])
        self.assertEqual(export_file["config_id"], "TD-001")
        for group in export_file["groups"]:
            self.assertEqual(group["drill_run_id"], run["drill_run_id"])

        # 父子标签版本被冻结
        self.assertEqual(export_file["parent_tag"], {"tag_id": "TAG-001", "version": "v1.0"})
        child_versions = {c["tag_id"]: c["version"] for c in export_file["child_tags"]}
        self.assertEqual(child_versions, {"TAG-002": "v1.0", "TAG-003": "v1.0"})

        # 分组口径与统计值
        self.assertEqual(export_file["group_basis"]["dimensions"], ["ATTR-001", "ATTR-004"])
        self.assertEqual(export_file["group_basis"]["stat_method"], "count")
        self.assertEqual(export_file["stat_method"], "count")
        self.assertGreater(export_file["group_count"], 0)
        self.assertEqual(len(export_file["groups"]), export_file["group_count"])

        # 原始结果编号：唯一、齐全、与分组行一一对应
        ids = export_file["source_result_ids"]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, [g["source_result_id"] for g in export_file["groups"]])
        for group in export_file["groups"]:
            self.assertTrue(group["source_result_id"].startswith(run["drill_run_id"]))
            self.assertIn("stat_value", group)
            self.assertIn("group_rule", group)

        # 层级快照包含下钻时的父子关系
        self.assertEqual(
            sorted(export_file["hierarchy_snapshot"]["TAG-001"]),
            ["TAG-002", "TAG-003"],
        )
        self.assertFalse(export_file["hierarchy_conflict"])

    def test_two_exports_from_same_run_keep_one_run_identity(self) -> None:
        compute_tag("TC-001")
        run = drill_tag("TD-001")
        first = prepare_export("TD-001", run["drill_run_id"])
        second = prepare_export("TD-001", run["drill_run_id"])
        self.assertNotEqual(first["export_id"], second["export_id"])
        self.assertEqual(first["drill_run_id"], second["drill_run_id"])
        self.assertEqual(first["groups"], second["groups"])

    def test_rerun_drill_creates_new_run_and_export_binds_to_requested_run(self) -> None:
        compute_tag("TC-001")
        first_run = drill_tag("TD-001")
        second_run = drill_tag("TD-001")
        self.assertNotEqual(first_run["drill_run_id"], second_run["drill_run_id"])

        export_file = prepare_export("TD-001", first_run["drill_run_id"])
        self.assertEqual(export_file["drill_run_id"], first_run["drill_run_id"])
        for group in export_file["groups"]:
            self.assertEqual(group["drill_run_id"], first_run["drill_run_id"])

    # ---------- 归档确认与下载/回查 ----------
    def _prepared_file(self) -> dict:
        compute_tag("TC-001")
        drill_tag("TD-001")
        return prepare_export("TD-001")

    def test_archive_confirms_once_and_enables_download_and_trace(self) -> None:
        export_file = self._prepared_file()
        result = submit_archive("ARC-001", copy.deepcopy(export_file), snapshot_id="SN-001")
        self.assertTrue(result["ok"])
        self.assertFalse(result["idempotent_replay"])
        self.assertEqual(result["status"], "confirmed")

        record = get_archive_record("ARC-001")
        self.assertEqual(record["status"], "confirmed")
        self.assertEqual(record["delivery_count"], 1)

        # 快照引用与文件互相指向
        self.assertEqual(record["export_file"]["snapshot_ref"]["archive_id"], "ARC-001")
        self.assertEqual(record["export_file"]["snapshot_ref"]["snapshot_id"], "SN-001")
        snapshot_row = store.find_by_field("tag_snapshot", "snapshot_id", "SN-001")
        self.assertEqual(snapshot_row["attachment"]["archive_id"], "ARC-001")
        self.assertTrue(snapshot_row["attachment"]["file_complete"])

        # 下载拿到的是同一份文件
        downloaded = download_archive_file("ARC-001")
        self.assertEqual(downloaded["export_file"]["export_id"], export_file["export_id"])
        self.assertEqual(downloaded["export_file"]["groups"], export_file["groups"])

        # 来源回查：每条分组行都能回到同一次下钻的原始结果，统计值一致
        trace = trace_archive_sources("ARC-001")
        self.assertTrue(trace["all_verified"])
        self.assertEqual(trace["total_groups"], trace["verified_groups"])
        self.assertEqual(trace["drill_run_id"], export_file["drill_run_id"])
        for line in trace["traces"]:
            self.assertTrue(line["same_drill_run"])
            self.assertTrue(line["stat_value_match"])

    def test_confirmed_archive_replay_returns_original_file(self) -> None:
        export_file = self._prepared_file()
        submit_archive("ARC-002", copy.deepcopy(export_file))

        replay = submit_archive("ARC-002", copy.deepcopy(export_file))
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["export_id"], export_file["export_id"])

        # 无论重试多少次，只交付一份快照
        submit_archive("ARC-002", copy.deepcopy(export_file))
        records = [r for r in list_archive_records() if r["archive_id"] == "ARC-002"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["delivery_count"], 1)
        self.assertEqual(records[0]["status"], "confirmed")

    def test_incomplete_attachment_rejected_and_blocks_download(self) -> None:
        export_file = self._prepared_file()

        broken = copy.deepcopy(export_file)
        del broken["groups"]
        with self.assertRaises(ArchiveError) as ctx:
            submit_archive("ARC-BAD", broken)
        self.assertEqual(ctx.exception.code, "incomplete_attachment")
        self.assertEqual(ctx.exception.status_code, 422)

        # 缺行字段
        broken_row = copy.deepcopy(export_file)
        del broken_row["groups"][0]["source_result_id"]
        with self.assertRaises(ArchiveError) as ctx2:
            submit_archive("ARC-BAD2", broken_row)
        self.assertEqual(ctx2.exception.code, "incomplete_attachment")

        # 未确认归档不可下载、不可回查
        self.assertIsNone(get_archive_record("ARC-BAD"))
        with self.assertRaises(ArchiveError) as ctx3:
            download_archive_file("ARC-NEVER")
        self.assertEqual(ctx3.exception.status_code, 404)

    def test_archiving_record_blocks_download_until_confirmed(self) -> None:
        export_file = self._prepared_file()
        with self.assertRaises(ArchiveError):
            submit_archive("ARC-WAIT", copy.deepcopy(export_file), simulate_failure="after_write")

        # 写入实际已完成，但确认未完成：下载与回查必须被拒绝
        record = get_archive_record("ARC-WAIT")
        self.assertEqual(record["status"], "archiving")
        with self.assertRaises(ArchiveError) as ctx:
            download_archive_file("ARC-WAIT")
        self.assertEqual(ctx.exception.code, "attachment_not_confirmed")
        with self.assertRaises(ArchiveError) as ctx2:
            trace_archive_sources("ARC-WAIT")
        self.assertEqual(ctx2.exception.code, "attachment_not_confirmed")

    def test_retry_after_write_failure_delivers_once_with_original_request(self) -> None:
        export_file = self._prepared_file()

        # 第一次：写入后失败，实际文件已落库
        with self.assertRaises(ArchiveError) as ctx:
            submit_archive("ARC-FAIL", copy.deepcopy(export_file), simulate_failure="after_write")
        self.assertEqual(ctx.exception.code, "archive_write_failed")
        persisted = get_archive_record("ARC-FAIL")
        self.assertEqual(persisted["export_id"], export_file["export_id"])
        self.assertEqual(persisted["attempts"], 1)

        # 运营用原归档请求补交（不带故障注入）
        retry = submit_archive("ARC-FAIL", copy.deepcopy(export_file))
        self.assertTrue(retry["ok"])
        self.assertEqual(retry["status"], "confirmed")
        self.assertFalse(retry["idempotent_replay"])

        record = get_archive_record("ARC-FAIL")
        self.assertEqual(record["attempts"], 2)
        self.assertEqual(record["delivery_count"], 1)

        # 再重试只返回原文件
        again = submit_archive("ARC-FAIL", copy.deepcopy(export_file))
        self.assertTrue(again["idempotent_replay"])
        self.assertEqual(get_archive_record("ARC-FAIL")["delivery_count"], 1)

        trace = trace_archive_sources("ARC-FAIL")
        self.assertTrue(trace["all_verified"])

    def test_before_confirm_interrupt_retry_resumes_and_delivers_once(self) -> None:
        export_file = self._prepared_file()
        with self.assertRaises(ArchiveError):
            submit_archive("ARC-INT", copy.deepcopy(export_file), simulate_failure="before_confirm")
        retry = submit_archive("ARC-INT", copy.deepcopy(export_file))
        self.assertEqual(retry["status"], "confirmed")
        records = [r for r in list_archive_records() if r["archive_id"] == "ARC-INT"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["delivery_count"], 1)
        downloaded = download_archive_file("ARC-INT")
        self.assertEqual(
            downloaded["export_file"]["snapshot_ref"]["archive_id"],
            "ARC-INT",
        )

    def test_archive_cannot_rebind_to_different_file(self) -> None:
        export_file = self._prepared_file()
        with self.assertRaises(ArchiveError):
            submit_archive("ARC-BIND", copy.deepcopy(export_file), simulate_failure="after_write")

        other = prepare_export("TD-001")
        self.assertNotEqual(other["export_id"], export_file["export_id"])
        with self.assertRaises(ArchiveError) as ctx:
            submit_archive("ARC-BIND", copy.deepcopy(other))
        self.assertEqual(ctx.exception.code, "archive_conflict")
        self.assertEqual(ctx.exception.status_code, 409)

    # ---------- 并发 ----------
    def test_concurrent_retries_deliver_single_snapshot(self) -> None:
        export_file = self._prepared_file()

        # 先确定性地制造一次写入后失败（文件已持久化、未确认）
        with self.assertRaises(ArchiveError) as ctx:
            submit_archive("ARC-CONC", copy.deepcopy(export_file), simulate_failure="after_write")
        self.assertEqual(ctx.exception.code, "archive_write_failed")

        errors: list[Exception] = []
        results: list[dict] = []

        def worker() -> None:
            try:
                # 运营并发用原归档请求补交，全部不带故障注入
                outcome = submit_archive("ARC-CONC", copy.deepcopy(export_file))
                results.append(outcome)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        barrier = threading.Barrier(6)

        def synced_worker() -> None:
            barrier.wait()
            worker()

        threads = [threading.Thread(target=synced_worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        records = [r for r in list_archive_records() if r["archive_id"] == "ARC-CONC"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "confirmed")
        self.assertEqual(records[0]["delivery_count"], 1)
        confirmed = [r for r in results if not r.get("idempotent_replay")]
        replays = [r for r in results if r.get("idempotent_replay")]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(len(replays), 5)

    # ---------- 层级冲突 ----------
    def test_hierarchy_change_after_export_keeps_old_levels_consistent(self) -> None:
        compute_tag("TC-001")
        drill_tag("TD-001")
        export_file = prepare_export("TD-001")

        # 导出准备完成后，子标签 TAG-002 转到新父级 TAG-003
        movement = move_child_tag("TAG-002", "TAG-003")
        self.assertEqual(movement["old_parent"], "TAG-001")
        self.assertEqual(movement["new_parent"], "TAG-003")

        # 文件本身保留导出时的旧层级，不被现行层级改写
        self.assertEqual(
            sorted(export_file["hierarchy_snapshot"]["TAG-001"]),
            ["TAG-002", "TAG-003"],
        )
        current = store.get_tag_children("TAG-001")
        self.assertEqual(current, ["TAG-003"])

        # 归档：失败后用原请求补交
        with self.assertRaises(ArchiveError):
            submit_archive("ARC-HIER", copy.deepcopy(export_file), simulate_failure="after_write")
        result = submit_archive("ARC-HIER", copy.deepcopy(export_file))
        self.assertTrue(result["hierarchy_conflict"])

        record = get_archive_record("ARC-HIER")
        self.assertTrue(record["hierarchy_conflict"])
        archived = record["export_file"]

        # 旧层级、统计值、快照引用相互一致
        self.assertEqual(
            sorted(archived["hierarchy_snapshot"]["TAG-001"]),
            ["TAG-002", "TAG-003"],
        )
        self.assertEqual(archived["conflict_detail"]["drill_child_tags"], ["TAG-002", "TAG-003"])
        self.assertEqual(archived["conflict_detail"]["current_child_tags"], ["TAG-003"])
        self.assertEqual(archived["snapshot_ref"]["archive_id"], "ARC-HIER")
        self.assertEqual(
            archived["hierarchy_conflict_at_archive"]["current_child_tags"],
            ["TAG-003"],
        )

        # 下载文件与回查仍然以旧下钻为准，统计值逐条一致
        downloaded = download_archive_file("ARC-HIER")["export_file"]
        self.assertEqual(downloaded["groups"], export_file["groups"])
        trace = trace_archive_sources("ARC-HIER")
        self.assertTrue(trace["all_verified"])
        self.assertTrue(trace["hierarchy_conflict"])
        self.assertEqual(trace["verified_groups"], trace["total_groups"])

    def test_trace_detects_stat_value_tampering(self) -> None:
        export_file = self._prepared_file()
        submit_archive("ARC-TAMPER", copy.deepcopy(export_file))
        record = get_archive_record("ARC-TAMPER")
        record["export_file"]["groups"][0]["stat_value"] = -999999
        trace = trace_archive_sources("ARC-TAMPER")
        self.assertFalse(trace["all_verified"])
        self.assertLess(trace["verified_groups"], trace["total_groups"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
