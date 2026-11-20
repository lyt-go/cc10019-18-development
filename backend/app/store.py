"""内存数据仓库：支持跨模块数据消费、版本快照和关联查询。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import threading
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 数据产物存储：跨模块共享的数据结果
        self._artifacts: dict[str, dict[str, Any]] = {}
        # 版本快照
        self._snapshots: dict[str, list[dict[str, Any]]] = {}
        # 计算结果缓存
        self._compute_cache: dict[str, dict[str, Any]] = {}
        # 标签层级注册表：parent_tag_id -> 子标签编号列表（现行父子层级）
        self._tag_hierarchy: dict[str, list[str]] = {}
        # 下钻分组明细导出文件：export_id -> 文件
        self._drill_exports: dict[str, dict[str, Any]] = {}
        # 快照归档记录：archive_id -> 归档记录（含状态机与交付的文件）
        self._snapshot_archives: dict[str, dict[str, Any]] = {}
        # 归档串行化锁：同一归档编号的并发重试只允许一个执行确认流程
        self._archive_locks: dict[str, threading.RLock] = {}
        self._archive_locks_guard = threading.Lock()
        self._reset_export_runtime()
        # 下一个自增ID
        self._next_ids: dict[str, int] = {
            name: max((int(row.get("id", 0)) for row in self._tables[name]), default=0) + 1
            for name in self._tables
        }

    def _reset_export_runtime(self) -> None:
        """初始化/重置导出归档相关的运行态存储。"""
        self._tag_hierarchy = {}
        for row in self._tables.get("tag_drill", []):
            parent = row.get("parent_tag")
            if not parent:
                continue
            children = self._tag_hierarchy.setdefault(parent, [])
            for child in row.get("child_tags", []):
                if child not in children:
                    children.append(child)
        self._drill_exports = {}
        self._snapshot_archives = {}
        self._archive_locks = {}

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def find_all_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get(field) == value]

    def insert(self, module: str, row: dict[str, Any]) -> dict[str, Any]:
        """插入一条记录并分配自增ID。"""
        if "id" not in row:
            row["id"] = self._next_ids.get(module, 1)
            self._next_ids[module] = row["id"] + 1
        self._tables.setdefault(module, []).append(dict(row))
        return row

    def update(self, module: str, entry_id: int, updates: dict[str, Any]) -> dict[str, Any] | None:
        """更新一条记录。"""
        row = self.find(module, entry_id)
        if row is None:
            return None
        row.update(updates)
        return row

    def delete(self, module: str, entry_id: int) -> bool:
        """删除一条记录。"""
        rows = self.rows(module)
        for i, row in enumerate(rows):
            if int(row.get("id", 0)) == entry_id:
                rows.pop(i)
                return True
        return False

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ========== 数据产物管理 ==========
    def set_artifact(self, artifact_type: str, artifact_id: str, data: dict[str, Any]) -> None:
        """存储跨模块数据产物。"""
        if artifact_type not in self._artifacts:
            self._artifacts[artifact_type] = {}
        self._artifacts[artifact_type][artifact_id] = data

    def get_artifact(self, artifact_type: str, artifact_id: str) -> dict[str, Any] | None:
        """获取跨模块数据产物。"""
        return self._artifacts.get(artifact_type, {}).get(artifact_id)

    def list_artifacts(self, artifact_type: str) -> list[dict[str, Any]]:
        """列出某类型的所有产物。"""
        return list(self._artifacts.get(artifact_type, {}).values())

    def get_artifact_keys(self, artifact_type: str) -> list[str]:
        """获取某类型产物的所有键。"""
        return list(self._artifacts.get(artifact_type, {}).keys())

    # ========== 快照管理 ==========
    def add_snapshot(self, snapshot_type: str, data: dict[str, Any]) -> None:
        """添加快照。"""
        self._snapshots.setdefault(snapshot_type, []).append(data)

    def get_latest_snapshot(self, snapshot_type: str) -> dict[str, Any] | None:
        """获取最新快照。"""
        snapshots = self._snapshots.get(snapshot_type, [])
        return snapshots[-1] if snapshots else None

    def list_snapshots(self, snapshot_type: str) -> list[dict[str, Any]]:
        """列出所有快照。"""
        return list(self._snapshots.get(snapshot_type, []))

    # ========== 计算缓存 ==========
    def set_compute_cache(self, cache_key: str, data: dict[str, Any]) -> None:
        """存储计算缓存。"""
        self._compute_cache[cache_key] = data

    def get_compute_cache(self, cache_key: str) -> dict[str, Any] | None:
        """获取计算缓存。"""
        return self._compute_cache.get(cache_key)

    def invalidate_compute_cache(self, prefix: str) -> None:
        """按前缀失效缓存。"""
        keys_to_remove = [k for k in self._compute_cache if k.startswith(prefix)]
        for k in keys_to_remove:
            del self._compute_cache[k]

    # ========== 标签层级注册表 ==========
    def get_tag_children(self, parent_tag: str) -> list[str]:
        """读取某父标签下现行的子标签编号列表。"""
        return list(self._tag_hierarchy.get(parent_tag, []))

    def move_tag_to_parent(self, child_tag: str, new_parent: str) -> dict[str, Any]:
        """把子标签转到新父级，返回迁移记录（旧父级可能为 None）。"""
        old_parent: str | None = None
        for parent, children in self._tag_hierarchy.items():
            if child_tag in children:
                old_parent = parent
                children.remove(child_tag)
                break
        bucket = self._tag_hierarchy.setdefault(new_parent, [])
        if child_tag not in bucket:
            bucket.append(child_tag)
        return {"child_tag": child_tag, "old_parent": old_parent, "new_parent": new_parent}

    def snapshot_hierarchy(self) -> dict[str, list[str]]:
        """导出现行层级的深拷贝快照。"""
        return {parent: list(children) for parent, children in self._tag_hierarchy.items()}

    # ========== 下钻分组明细导出文件 ==========
    def save_drill_export(self, export_file: dict[str, Any]) -> None:
        self._drill_exports[export_file["export_id"]] = export_file

    def get_drill_export(self, export_id: str) -> dict[str, Any] | None:
        return self._drill_exports.get(export_id)

    def list_drill_exports(self) -> list[dict[str, Any]]:
        return list(self._drill_exports.values())

    # ========== 快照归档 ==========
    def get_archive(self, archive_id: str) -> dict[str, Any] | None:
        return self._snapshot_archives.get(archive_id)

    def save_archive(self, record: dict[str, Any]) -> None:
        self._snapshot_archives[record["archive_id"]] = record

    def list_archives(self) -> list[dict[str, Any]]:
        return list(self._snapshot_archives.values())

    def archive_lock(self, archive_id: str) -> threading.RLock:
        """获取某归档编号专属的可重入锁，串行化并发重试。"""
        with self._archive_locks_guard:
            lock = self._archive_locks.get(archive_id)
            if lock is None:
                lock = threading.RLock()
                self._archive_locks[archive_id] = lock
            return lock

    # ========== 跨模块关联查询 ==========
    def join(self, left_module: str, right_module: str, left_field: str, right_field: str) -> list[dict[str, Any]]:
        """执行简单的内存join。"""
        results: list[dict[str, Any]] = []
        left_rows = self.rows(left_module)
        right_rows = self.rows(right_module)
        right_index: dict[Any, list[dict[str, Any]]] = {}
        for r in right_rows:
            key = r.get(right_field)
            right_index.setdefault(key, []).append(r)
        for l in left_rows:
            key = l.get(left_field)
            for r in right_index.get(key, []):
                merged = dict(l)
                for rk, rv in r.items():
                    if rk not in merged:
                        merged[rk] = rv
                    else:
                        merged[f"{right_module}.{rk}"] = rv
                results.append(merged)
        return results


def query_filters(request: Any) -> dict[str, Any]:
    """从请求查询参数中提取过滤条件（排除分页字段）。"""
    params = dict(request.query_params)
    return {k: v for k, v in params.items() if k not in ("page", "size")}


store = Store()
