from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import require_auth
from database import get_db
import backup_service

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


@router.get("/backups")
def list_backups():
    return backup_service.list_backups()


@router.post("/backups", status_code=201)
def create_backup(db: Session = Depends(get_db)):
    return backup_service.create_backup(db, trigger="manual")


class DeleteBackupsBody(BaseModel):
    ids: list[str]


@router.delete("/backups", status_code=204)
def delete_backups(body: DeleteBackupsBody):
    for backup_id in body.ids:
        try:
            path = backup_service.get_backup_path(backup_id)
            path.unlink()
        except ValueError:
            pass  # skip missing IDs silently


@router.get("/backups/{backup_id}/download")
def download_backup(backup_id: str):
    try:
        path = backup_service.get_backup_path(backup_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(
        path=str(path),
        media_type="application/zip",
        filename=path.name,
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


@router.post("/backups/{backup_id}/restore")
def restore_backup(backup_id: str, db: Session = Depends(get_db)):
    try:
        path = backup_service.get_backup_path(backup_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Backup not found")

    # Safety snapshot of current state before overwriting
    backup_service.create_backup(db, trigger="pre-restore")

    try:
        backup_service.restore_from_path(path, db)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {"ok": True}


@router.post("/backups/upload", status_code=201)
async def upload_backup(file: UploadFile = File(...)):
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    try:
        original_info = backup_service.validate_zip_bytes(content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return backup_service.save_uploaded_backup(content, original_info)
