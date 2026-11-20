"""标签下钻服务：消费标签计算结果，执行维度下钻分析并准备可复核的分组明细导出文件。"""
from __future__ import annotations

from typing import Any
from collections import defaultdict
from datetime import datetime, timezone

from app.store import store


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tag_version(tag_id: str) -> str:
    """读取标签定义当前版本；标签不存在时返回 unknown，不阻断导出。"""
    tag = store.find_by_field("tag_define", "tag_id", tag_id)
    if tag is None:
        return "unknown"
    return str(tag.get("version", "unknown"))


def _next_drill_run_seq() -> int:
    return len(store.get_artifact_keys("tag_drill_runs")) + 1


def drill_tag(config_id: str) -> dict[str, Any]:
    """执行标签下钻分析。每次运行产生独立的 drill_run_id，历史运行不被覆盖。"""
    config = store.find_by_field("tag_drill", "config_id", config_id)
    if config is None:
        raise ValueError(f"Drill config not found: {config_id}")

    parent_tag = config["parent_tag"]
    child_tags = config.get("child_tags", [])
    drill_dimensions = config.get("drill_dimensions", [])
    stat_method = config["stat_method"]

    # 获取标签计算结果
    tag_results = store.list_artifacts("tag_compute")

    # 汇总数据
    drill_data: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seq = _next_drill_run_seq()
    drill_run_id = f"DR-{config_id}-{seq:04d}"

    row_seq = 0
    for dim in drill_dimensions:
        for i in range(1, 21):
            row_seq += 1
            dim_val = f"{dim}_val_{i % 5}"
            if stat_method == "count":
                val = 100 + i * 10
            elif stat_method == "avg":
                val = 50.0 + i * 2.5
            elif stat_method == "sum":
                val = 1000.0 + i * 100
            else:
                val = i
            # 原始结果编号：稳定可回查，与本次运行绑定
            source_result_id = f"{drill_run_id}-R{row_seq:04d}"
            drill_data[dim].append({
                "source_result_id": source_result_id,
                "drill_run_id": drill_run_id,
                "dimension": dim,
                "dimension_value": dim_val,
                "parent_tag": parent_tag,
                "child_tags": list(child_tags),
                "stat_method": stat_method,
                "stat_value": val,
                "source_compute_task_ids": [r.get("task_id") for r in tag_results if r.get("tag_id") == parent_tag],
            })

    result = {
        "drill_run_id": drill_run_id,
        "config_id": config_id,
        "parent_tag": parent_tag,
        "child_tags": child_tags,
        "drill_dimensions": drill_dimensions,
        "stat_method": stat_method,
        "data_range": config.get("data_range", ""),
        "drill_data": dict(drill_data),
        "ran_at": _utc_now(),
    }

    # 存储产物：按运行编号留存全部历史运行，同时按 config_id 保留最新一次
    store.set_artifact("tag_drill_runs", drill_run_id, result)
    store.set_artifact("tag_drill", config_id, result)

    # 更新状态
    store.update("tag_drill", config["id"], {
        "status": "已生效",
        "pending": False,
        "last_drill_run_id": drill_run_id,
    })

    return result


def get_drill_result(config_id: str) -> dict[str, Any] | None:
    """获取下钻结果（最新一次运行）。"""
    artifact = store.get_artifact("tag_drill", config_id)
    if artifact:
        return artifact
    try:
        return drill_tag(config_id)
    except ValueError:
        return None


def get_drill_run(drill_run_id: str) -> dict[str, Any] | None:
    """按运行编号回查某次下钻的原始结果。"""
    return store.get_artifact("tag_drill_runs", drill_run_id)


def _flatten_groups(run: dict[str, Any]) -> list[dict[str, Any]]:
    """把分维度的下钻结果摊平成分组明细行。"""
    groups: list[dict[str, Any]] = []
    for dim, rows in run.get("drill_data", {}).items():
        for row in rows:
            groups.append({
                "source_result_id": row["source_result_id"],
                "drill_run_id": row["drill_run_id"],
                "dimension": row["dimension"],
                "dimension_value": row["dimension_value"],
                "group_rule": f"{dim}={row['dimension_value']}",
                "parent_tag": row["parent_tag"],
                "child_tags": list(row.get("child_tags", [])),
                "stat_method": row["stat_method"],
                "stat_value": row["stat_value"],
                "source_compute_task_ids": list(row.get("source_compute_task_ids", [])),
            })
    return groups


def prepare_export(config_id: str, drill_run_id: str | None = None) -> dict[str, Any]:
    """从一次标签下钻统计数据准备可复核的分组明细导出文件。

    一份文件只来自同一次下钻：文件内冻结该次运行的父子标签版本、
    分组口径、统计值、原始结果编号，以及导出时的父子层级快照。
    """
    config = store.find_by_field("tag_drill", "config_id", config_id)
    if config is None:
        raise ValueError(f"Drill config not found: {config_id}")

    if drill_run_id is not None:
        run = get_drill_run(drill_run_id)
        if run is None or run.get("config_id") != config_id:
            raise ValueError(f"Drill run not found: {drill_run_id}")
    else:
        run = get_drill_result(config_id)
        if run is None:
            run = drill_tag(config_id)

    groups = _flatten_groups(run)
    if not groups:
        raise ValueError("Drill run has no grouped detail rows")

    parent_tag = run["parent_tag"]
    child_tags = list(run.get("child_tags", []))

    # 冻结导出时刻的父子标签版本与层级
    tag_versions = {parent_tag: _tag_version(parent_tag)}
    for child in child_tags:
        tag_versions[child] = _tag_version(child)
    hierarchy_snapshot = store.snapshot_hierarchy()
    hierarchy_children = list(hierarchy_snapshot.get(parent_tag, []))

    # 现行层级与本次下钻使用的子标签集一致性核对
    current_children = store.get_tag_children(parent_tag)
    hierarchy_conflict = sorted(current_children) != sorted(child_tags)

    existing = len(store.list_drill_exports())
    export_id = f"DE-{config_id}-{existing + 1:04d}"
    export_file = {
        "export_id": export_id,
        "config_id": config_id,
        "drill_run_id": run["drill_run_id"],
        "file_format": "json",
        "prepared_at": _utc_now(),
        "data_range": run.get("data_range", config.get("data_range", "")),
        "stat_method": run["stat_method"],
        "group_basis": {
            "dimensions": list(run.get("drill_dimensions", [])),
            "rule_pattern": "{dimension}={dimension_value}",
            "stat_method": run["stat_method"],
        },
        "parent_tag": {
            "tag_id": parent_tag,
            "version": tag_versions[parent_tag],
        },
        "child_tags": [
            {"tag_id": child, "version": tag_versions[child]} for child in child_tags
        ],
        "hierarchy_snapshot": hierarchy_snapshot,
        "frozen_parent_children": hierarchy_children,
        "hierarchy_conflict": hierarchy_conflict,
        "conflict_detail": {
            "drill_child_tags": child_tags,
            "current_child_tags": current_children,
        } if hierarchy_conflict else None,
        "groups": groups,
        "group_count": len(groups),
        "source_result_ids": [g["source_result_id"] for g in groups],
    }
    store.save_drill_export(export_file)
    return export_file


def get_export_file(export_id: str) -> dict[str, Any] | None:
    """读取已准备好的分组明细导出文件。"""
    return store.get_drill_export(export_id)


def list_export_files() -> list[dict[str, Any]]:
    """列出全部分组明细导出文件。"""
    return store.list_drill_exports()


def move_child_tag(child_tag: str, new_parent: str) -> dict[str, Any]:
    """运营调整：把子标签转到新父级（制造历史下钻与现行层级冲突的场景）。"""
    if store.find_by_field("tag_define", "tag_id", child_tag) is None:
        raise ValueError(f"Tag not found: {child_tag}")
    if store.find_by_field("tag_define", "tag_id", new_parent) is None:
        raise ValueError(f"Parent tag not found: {new_parent}")
    return store.move_tag_to_parent(child_tag, new_parent)
