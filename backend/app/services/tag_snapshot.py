"""标签快照服务：全量快照，以及下钻分组明细文件的归档、下载与来源回查。

归档契约：
- 归档请求携带完整的分组明细文件，同一个归档编号（archive_id）无论重试多少次，
  最多只确认交付一份快照附件；
- 快照只有在附件完整确认（confirmed）之后才允许下载与下钻来源回查；
- 已经确认的归档再次收到请求时直接返回原文件，不重复写入、不改变统计值与层级。
"""
from __future__ import annotations

import copy
from typing import Any
from datetime import datetime, timezone

from app.store import store


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 归档附件必须具备的顶层字段（附件完整性校验口径）
REQUIRED_FILE_FIELDS = (
    "export_id",
    "drill_run_id",
    "config_id",
    "parent_tag",
    "child_tags",
    "group_basis",
    "hierarchy_snapshot",
    "groups",
    "source_result_ids",
)

# 每个分组行必须具备的可复核字段
REQUIRED_GROUP_FIELDS = (
    "source_result_id",
    "drill_run_id",
    "dimension",
    "dimension_value",
    "group_rule",
    "stat_method",
    "stat_value",
)


class ArchiveError(ValueError):
    """归档业务错误：携带稳定的错误码，路由层映射为对应 HTTP 状态。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def create_snapshot(snapshot_id: str) -> dict[str, Any]:
    """创建标签快照，消费标签计算和分群结果。"""
    snapshot = store.find_by_field("tag_snapshot", "snapshot_id", snapshot_id)
    if snapshot is None:
        raise ValueError(f"Snapshot not found: {snapshot_id}")

    # 获取所有标签定义
    tags = store.rows("tag_define")
    # 获取所有分群
    segments = store.rows("user_segment")
    # 获取标签计算结果
    compute_results = store.list_artifacts("tag_compute")

    # 计算用户基数（从标签计算结果汇总）
    total_users = 0
    for result in compute_results:
        total_users = max(total_users, result.get("result_count", 0))
    if total_users == 0:
        total_users = 50000  # 默认值

    # 汇总快照数据
    snapshot_data = {
        "snapshot_id": snapshot_id,
        "snapshot_date": snapshot["snapshot_date"],
        "user_count": total_users,
        "tag_count": len(tags),
        "segment_count": len(segments),
        "tags": [{"tag_id": t["tag_id"], "tag_name": t["tag_name"]} for t in tags],
        "segments": [{"segment_id": s["segment_id"], "segment_name": s["segment_name"]} for s in segments],
        "compute_results": [
            {
                "task_id": r.get("task_id"),
                "tag_id": r.get("tag_id"),
                "result_count": r.get("result_count"),
                "compute_scope": r.get("compute_scope"),
            }
            for r in compute_results
        ],
        "snapshot_path": snapshot["snapshot_path"],
        "storage_format": snapshot["storage_format"],
    }

    # 存储快照产物
    store.set_artifact("tag_snapshot", snapshot_id, snapshot_data)
    store.add_snapshot("tag_snapshot", snapshot_data)

    # 更新快照记录
    store.update("tag_snapshot", snapshot["id"], {
        "status": "已完成",
        "pending": False,
        "user_count": total_users,
        "tag_count": len(tags),
        "segment_count": len(segments),
    })

    return snapshot_data


def get_snapshot(snapshot_id: str) -> dict[str, Any] | None:
    """获取标签快照。"""
    artifact = store.get_artifact("tag_snapshot", snapshot_id)
    if artifact:
        return artifact
    try:
        return create_snapshot(snapshot_id)
    except ValueError:
        return None


def compare_snapshots(snapshot_id_a: str, snapshot_id_b: str) -> dict[str, Any]:
    """对比两个快照的差异。"""
    snap_a = get_snapshot(snapshot_id_a)
    snap_b = get_snapshot(snapshot_id_b)
    if snap_a is None or snap_b is None:
        raise ValueError("Snapshot not found")

    return {
        "snapshot_a": snapshot_id_a,
        "snapshot_b": snapshot_id_b,
        "user_count_diff": snap_b.get("user_count", 0) - snap_a.get("user_count", 0),
        "tag_count_diff": snap_b.get("tag_count", 0) - snap_a.get("tag_count", 0),
        "segment_count_diff": snap_b.get("segment_count", 0) - snap_a.get("segment_count", 0),
        "date_a": snap_a.get("snapshot_date"),
        "date_b": snap_b.get("snapshot_date"),
    }


def list_snapshots() -> list[dict[str, Any]]:
    """列出所有标签快照。"""
    return store.list_snapshots("tag_snapshot")


# ========== 下钻分组明细文件归档 ==========
def _validate_archive_file(export_file: dict[str, Any]) -> None:
    """附件完整性校验：缺字段或分组行不可复核时拒绝归档。"""
    if not isinstance(export_file, dict):
        raise ArchiveError("incomplete_attachment", "归档附件必须是完整的分组明细文件对象", 422)
    missing = [f for f in REQUIRED_FILE_FIELDS if f not in export_file]
    if missing:
        raise ArchiveError(
            "incomplete_attachment",
            f"归档附件缺少必填字段: {', '.join(missing)}",
            422,
        )
    groups = export_file.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ArchiveError("incomplete_attachment", "归档附件的分组明细 groups 不能为空", 422)
    for index, group in enumerate(groups):
        missing_row = [f for f in REQUIRED_GROUP_FIELDS if f not in group]
        if missing_row:
            raise ArchiveError(
                "incomplete_attachment",
                f"第 {index + 1} 行分组明细缺少字段: {', '.join(missing_row)}",
                422,
            )
    ids = export_file.get("source_result_ids")
    if not isinstance(ids, list) or len(ids) != len(groups):
        raise ArchiveError(
            "incomplete_attachment",
            "原始结果编号数量必须与分组明细行数一致",
            422,
        )
    if len(set(ids)) != len(ids):
        raise ArchiveError("incomplete_attachment", "原始结果编号存在重复，附件不可复核", 422)


def _rebuild_conflict(export_file: dict[str, Any]) -> dict[str, Any]:
    """归档时刻重新核对现行层级与文件冻结层级的冲突（文件内容本身不被改写）。"""
    parent_tag = export_file["parent_tag"]
    parent_id = parent_tag["tag_id"] if isinstance(parent_tag, dict) else parent_tag
    drill_child_tags = [
        child["tag_id"] if isinstance(child, dict) else child
        for child in export_file.get("child_tags", [])
    ]
    current_children = store.get_tag_children(parent_id)
    conflict = sorted(current_children) != sorted(drill_child_tags)
    return {
        "conflict": conflict,
        "drill_child_tags": drill_child_tags,
        "current_child_tags": current_children,
    }


def _next_snapshot_id() -> str:
    existing = {row["snapshot_id"] for row in store.rows("tag_snapshot")}
    seq = len(existing) + 1
    snapshot_id = f"SN-ARC-{seq:03d}"
    while snapshot_id in existing:
        seq += 1
        snapshot_id = f"SN-ARC-{seq:03d}"
    return snapshot_id


def _ensure_snapshot_row(snapshot_id: str, archive_id: str, export_file: dict[str, Any]) -> dict[str, Any]:
    """挂接快照登记记录：存在则复用，不存在则新建一条。"""
    row = store.find_by_field("tag_snapshot", "snapshot_id", snapshot_id)
    attachment = {
        "archive_id": archive_id,
        "export_id": export_file["export_id"],
        "drill_run_id": export_file["drill_run_id"],
        "file_complete": True,
        "confirmed_at": _utc_now(),
    }
    if row is None:
        row = store.insert("tag_snapshot", {
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "snapshot_id": snapshot_id,
            "snapshot_date": datetime.now(timezone.utc).date().isoformat(),
            "user_count": 0,
            "tag_count": len(export_file.get("child_tags", [])) + 1,
            "segment_count": 0,
            "snapshot_path": f"/snapshots/archives/{archive_id}/{export_file['export_id']}.json",
            "storage_format": "json",
            "retention_days": 90,
            "archive_id": archive_id,
            "attachment": attachment,
        })
    else:
        store.update("tag_snapshot", row["id"], {
            "status": "已完成",
            "pending": False,
            "archive_id": archive_id,
            "attachment": attachment,
        })
        row = store.find("tag_snapshot", row["id"])
    return row  # type: ignore[return-value]


def submit_archive(
    archive_id: str,
    export_file: dict[str, Any],
    snapshot_id: str | None = None,
    simulate_failure: str | None = None,
) -> dict[str, Any]:
    """接收一次快照归档请求并幂等交付。

    simulate_failure:
    - "after_write"：附件已持久化之后、确认之前制造失败（模拟写入实际完成但响应失败）；
    - "before_confirm"：文件准备完成与快照确认之间中断（重试后应继续完成）。
    """
    lock = store.archive_lock(archive_id)
    with lock:
        existing = store.get_archive(archive_id)

        # 已确认：幂等返回原文件，绝不重复交付
        if existing is not None and existing.get("status") == "confirmed":
            return {
                "ok": True,
                "idempotent_replay": True,
                "archive_id": archive_id,
                "status": "confirmed",
                "snapshot_id": existing.get("snapshot_id"),
                "export_id": existing["export_file"]["export_id"],
                "export_file": existing["export_file"],
            }

        # 附件完整性校验（含重试：上次中断时附件尚未确认，需要重新校验请求）
        _validate_archive_file(export_file)

        # 一份文件必须来自同一次下钻：同归档编号的重试必须携带同一运行/同一文件
        if existing is not None:
            if existing.get("export_id") != export_file["export_id"]:
                raise ArchiveError(
                    "archive_conflict",
                    f"归档编号 {archive_id} 已绑定文件 {existing.get('export_id')}，不能替换为 {export_file['export_id']}",
                    409,
                )
            # 复用已持久化的文件，保证“已确认后仍返回原文件”
            confirmed_file = existing["export_file"]
            failure_already_consumed = bool(existing.get("failure_injected"))
        else:
            confirmed_file = copy.deepcopy(export_file)
            failure_already_consumed = False
            record = {
                "archive_id": archive_id,
                "status": "archiving",
                "export_id": confirmed_file["export_id"],
                "drill_run_id": confirmed_file["drill_run_id"],
                "export_file": confirmed_file,
                "snapshot_id": snapshot_id,
                "created_at": _utc_now(),
                "failure_injected": False,
                "attempts": 0,
            }
            store.save_archive(record)
            existing = record

        existing["attempts"] = int(existing.get("attempts", 0)) + 1

        # 写入阶段（即使随后响应失败，文件已经落库，重试走幂等路径）
        store.save_archive(existing)

        should_fail = (
            simulate_failure in ("after_write", "before_confirm")
            and not failure_already_consumed
        )
        if should_fail:
            existing["failure_injected"] = True
            existing["last_failure"] = simulate_failure
            store.save_archive(existing)
            raise ArchiveError(
                "archive_write_failed",
                "归档写入后确认前发生故障，请使用相同归档编号重试",
                500,
            )

        # 确认阶段：重新核对历史下钻与现行层级的冲突，冻结在交付视图里。
        # 注意：hierarchy_snapshot 等旧层级数据保持导出时原样，只补充冲突标注。
        conflict = _rebuild_conflict(confirmed_file)
        target_snapshot_id = snapshot_id or existing.get("snapshot_id") or _next_snapshot_id()

        confirmed_file["hierarchy_conflict"] = conflict["conflict"]
        confirmed_file["conflict_detail"] = {
            "drill_child_tags": conflict["drill_child_tags"],
            "current_child_tags": conflict["current_child_tags"],
        } if conflict["conflict"] else None
        # 写入快照引用，使文件内的快照引用与快照附件互相指向
        confirmed_file["snapshot_ref"] = {
            "archive_id": archive_id,
            "snapshot_id": target_snapshot_id,
            "confirmed_at": _utc_now(),
        }
        confirmed_file["hierarchy_conflict_at_archive"] = conflict

        snapshot_row = _ensure_snapshot_row(target_snapshot_id, archive_id, confirmed_file)

        existing.update({
            "status": "confirmed",
            "snapshot_id": target_snapshot_id,
            "confirmed_at": _utc_now(),
            "hierarchy_conflict": conflict["conflict"],
            "snapshot_row_id": snapshot_row["id"],
            "delivery_count": 1,
        })
        store.save_archive(existing)

        return {
            "ok": True,
            "idempotent_replay": False,
            "archive_id": archive_id,
            "status": "confirmed",
            "snapshot_id": target_snapshot_id,
            "export_id": confirmed_file["export_id"],
            "hierarchy_conflict": conflict["conflict"],
            "export_file": confirmed_file,
        }


def _get_confirmed_archive(archive_id: str) -> dict[str, Any]:
    record = store.get_archive(archive_id)
    if record is None:
        raise ArchiveError("archive_not_found", f"归档不存在: {archive_id}", 404)
    if record.get("status") != "confirmed":
        raise ArchiveError(
            "attachment_not_confirmed",
            "快照附件尚未完整确认，暂不可下载或回查",
            409,
        )
    return record


def download_archive_file(archive_id: str) -> dict[str, Any]:
    """通过归档编号下载已确认快照的分组明细文件。"""
    record = _get_confirmed_archive(archive_id)
    return {
        "archive_id": archive_id,
        "snapshot_id": record["snapshot_id"],
        "status": record["status"],
        "export_file": record["export_file"],
    }


def trace_archive_sources(archive_id: str) -> dict[str, Any]:
    """下钻来源回查：归档分组行 -> 当次下钻原始结果，逐条核对统计值与原始结果编号。"""
    record = _get_confirmed_archive(archive_id)
    export_file = record["export_file"]
    drill_run_id = export_file["drill_run_id"]
    drill_run = store.get_artifact("tag_drill_runs", drill_run_id)
    if drill_run is None:
        raise ArchiveError(
            "source_not_found",
            f"下钻运行结果已不可回查: {drill_run_id}",
            410,
        )

    # 以原始结果编号为主键建立当次下钻的原始结果索引
    source_index: dict[str, dict[str, Any]] = {}
    for rows in drill_run.get("drill_data", {}).values():
        for row in rows:
            source_index[row["source_result_id"]] = row

    traces: list[dict[str, Any]] = []
    all_match = True
    for group in export_file["groups"]:
        source_result_id = group["source_result_id"]
        source = source_index.get(source_result_id)
        stat_match = bool(source and source.get("stat_value") == group["stat_value"])
        run_match = bool(source and source.get("drill_run_id") == drill_run_id)
        line_match = stat_match and run_match
        all_match = all_match and line_match
        traces.append({
            "source_result_id": source_result_id,
            "drill_run_id": drill_run_id,
            "group_rule": group["group_rule"],
            "archived_stat_value": group["stat_value"],
            "source_stat_value": source.get("stat_value") if source else None,
            "stat_value_match": stat_match,
            "same_drill_run": run_match,
            "verified": line_match,
        })

    return {
        "archive_id": archive_id,
        "snapshot_id": record["snapshot_id"],
        "export_id": export_file["export_id"],
        "drill_run_id": drill_run_id,
        "total_groups": len(export_file["groups"]),
        "verified_groups": sum(1 for t in traces if t["verified"]),
        "all_verified": all_match,
        "hierarchy_conflict": record.get("hierarchy_conflict", export_file.get("hierarchy_conflict", False)),
        "traces": traces,
    }


def get_archive_record(archive_id: str) -> dict[str, Any] | None:
    """查看归档状态（不要求已确认，供运营排查中断/重试）。"""
    return store.get_archive(archive_id)


def list_archive_records() -> list[dict[str, Any]]:
    """列出全部归档记录。"""
    return store.list_archives()
