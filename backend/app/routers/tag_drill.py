"""标签下钻路由：执行下钻分析、准备分组明细导出并获取结果。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.schemas import ActionPayload, ActionResult, DrillExportPayload, TagMovePayload
from app.services.tag_drill import (
    drill_tag,
    get_drill_result,
    get_drill_run,
    get_export_file,
    list_export_files,
    move_child_tag,
    prepare_export,
)
from app.services.tag_snapshot import ArchiveError
from app.store import store

router = APIRouter(prefix="/api/tag-drill", tags=["tag_drill"])


@router.get("")
def index(page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("tag_drill")
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    rows = store.rows("tag_drill")
    return {"items": rows, "total": len(rows)}


@router.get("/exports")
def list_exports() -> dict[str, Any]:
    """列出已准备的分组明细导出文件。"""
    items = list_export_files()
    return {"items": items, "total": len(items)}


@router.post("/{config_id}/prepare-export")
def prepare_drill_export(config_id: str, payload: DrillExportPayload | None = None) -> ActionResult:
    """从一次标签下钻统计数据准备可复核的分组明细文件。"""
    drill_run_id = payload.drill_run_id if payload is not None else None
    try:
        export_file = prepare_export(config_id, drill_run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return ActionResult(ok=True, message="Export prepared", entry=export_file)


@router.get("/exports/{export_id}")
def download_export(export_id: str) -> JSONResponse:
    """下载已准备好的分组明细导出文件。"""
    export_file = get_export_file(export_id)
    if export_file is None:
        raise HTTPException(status_code=404, detail="Export file not found")
    filename = f"{export_id}.json"
    return JSONResponse(
        content=export_file,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/runs/{drill_run_id}")
def drill_run_detail(drill_run_id: str) -> dict[str, Any]:
    """按运行编号回查某次下钻的原始结果。"""
    run = get_drill_run(drill_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Drill run not found")
    return run


@router.post("/move-tag")
def move_tag(payload: TagMovePayload) -> ActionResult:
    """把子标签转到新父级（运营层级调整）。"""
    try:
        movement = move_child_tag(payload.child_tag, payload.new_parent)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return ActionResult(ok=True, message="Tag moved", entry=movement)


@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("tag_drill", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{config_id}/run")
def run(config_id: str) -> ActionResult:
    try:
        result = drill_tag(config_id)
        return ActionResult(ok=True, message="Drilled", entry=result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{config_id}/result")
def result(config_id: str) -> dict[str, Any]:
    res = get_drill_result(config_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("tag_drill", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
