"""标签快照路由：创建快照、归档下钻分组明细文件并提供下载与来源回查。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.schemas import ActionPayload, ActionResult, ArchiveSubmitPayload
from app.services.tag_snapshot import (
    ArchiveError,
    compare_snapshots,
    create_snapshot,
    download_archive_file,
    get_archive_record,
    get_snapshot,
    list_archive_records,
    list_snapshots,
    submit_archive,
    trace_archive_sources,
)
from app.store import store

router = APIRouter(prefix="/api/tag-snapshot", tags=["tag_snapshot"])


@router.get("")
def index(page: int = 1, size: int = 20) -> dict[str, Any]:
    rows = store.rows("tag_snapshot")
    total = len(rows)
    start = (page - 1) * size
    return {"items": rows[start : start + size], "total": total, "page": page, "size": size}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    rows = store.rows("tag_snapshot")
    return {"items": rows, "total": len(rows)}


@router.get("/archives")
def archives() -> dict[str, Any]:
    """列出全部归档记录（含中断待重试状态）。"""
    items = list_archive_records()
    return {"items": items, "total": len(items)}


@router.post("/archives")
def archive_submit(payload: ArchiveSubmitPayload) -> ActionResult:
    """接收完整分组明细文件并归档到快照；同一归档编号幂等交付一份附件。"""
    try:
        result = submit_archive(
            archive_id=payload.archive_id,
            export_file=payload.export_file,
            snapshot_id=payload.snapshot_id,
            simulate_failure=payload.simulate_failure,
        )
    except ArchiveError as e:
        raise HTTPException(status_code=e.status_code, detail={"code": e.code, "message": e.message})
    return ActionResult(ok=result["ok"], message="Archived" if not result.get("idempotent_replay") else "Replayed", entry=result)


@router.get("/archives/{archive_id}")
def archive_detail(archive_id: str) -> dict[str, Any]:
    """查看归档状态（未确认也可查询，便于排查中断）。"""
    record = get_archive_record(archive_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Archive not found")
    return record


@router.get("/archives/{archive_id}/download")
def archive_download(archive_id: str) -> JSONResponse:
    """通过归档编号下载快照附件；附件未完整确认时拒绝下载。"""
    try:
        result = download_archive_file(archive_id)
    except ArchiveError as e:
        raise HTTPException(status_code=e.status_code, detail={"code": e.code, "message": e.message})
    export_file = result["export_file"]
    filename = f"{archive_id}_{export_file['export_id']}.json"
    return JSONResponse(
        content=result,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/archives/{archive_id}/trace")
def archive_trace(archive_id: str) -> dict[str, Any]:
    """下钻来源回查：逐条核对归档统计值与当次下钻原始结果编号。"""
    try:
        return trace_archive_sources(archive_id)
    except ArchiveError as e:
        raise HTTPException(status_code=e.status_code, detail={"code": e.code, "message": e.message})


@router.get("/list/all")
def all_snapshots() -> list[dict[str, Any]]:
    return list_snapshots()


@router.get("/{entry_id}")
def detail(entry_id: int) -> dict[str, Any]:
    row = store.find("tag_snapshot", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


@router.post("/{snapshot_id}/create")
def create(snapshot_id: str) -> ActionResult:
    try:
        result = create_snapshot(snapshot_id)
        return ActionResult(ok=True, message="Created", entry=result)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{snapshot_id}/result")
def result(snapshot_id: str) -> dict[str, Any]:
    res = get_snapshot(snapshot_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Result not found")
    return res


@router.get("/compare/{snapshot_id_a}/{snapshot_id_b}")
def compare(snapshot_id_a: str, snapshot_id_b: str) -> dict[str, Any]:
    try:
        return compare_snapshots(snapshot_id_a, snapshot_id_b)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    row = store.find("tag_snapshot", entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    action = payload.action
    row["status"] = action
    row["pending"] = False
    return ActionResult(ok=True, message=action, entry=row)
