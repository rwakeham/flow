from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import require_auth
from database import get_db
import backup_service
import change_log
import settings_store as st

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

BACKUP_SETTINGS = (st.BACKUP_DAILY, st.BACKUP_KEEP, st.BACKUP_HOUR)


@router.get("/backups")
def list_backups():
    return backup_service.list_backups()


# ── The daily backup's settings (Settings → Backups) ─────────────────────────

def settings_payload() -> dict:
    """Each setting with its value and where it comes from (.env wins while set there), what is in
    effect, and when the newest scheduled backup was taken."""
    out = {key: {"value": st.setting(key), "source": st.setting_source(key), "env_var": st.SETTING_ENV[key]}
           for key in BACKUP_SETTINGS}
    out["effective"] = {"daily": st.backup_daily_flag(), "keep": st.backup_keep(), "hour": st.backup_hour()}
    out["last_scheduled"] = backup_service.last_scheduled(backup_service.list_backups())
    return out


@router.get("/backups/settings")
def backup_settings():
    return settings_payload()


class BackupSettingsBody(BaseModel):
    daily: bool | None = None
    keep: int | None = Field(default=None, ge=0, le=st.BACKUP_KEEP_MAX)
    hour: int | None = Field(default=None, ge=0, le=23)


@router.put("/backups/settings")
def put_backup_settings(body: BackupSettingsBody, db: Session = Depends(get_db)):
    changes = {st.BACKUP_DAILY: None if body.daily is None else ("1" if body.daily else "0"),
               st.BACKUP_KEEP: None if body.keep is None else str(body.keep),
               st.BACKUP_HOUR: None if body.hour is None else str(body.hour)}
    for key, value in changes.items():
        if value is not None and st.setting_source(key) == "env":
            raise HTTPException(status_code=409, detail=st.env_locked_message(st.SETTING_ENV[key]))
    changed = {key: value for key, value in changes.items() if value is not None}
    if changed:
        change_log.record(db, "backup", "settings", "Backup settings changed: " + ", ".join(f"{k.removeprefix('backup_')} = {v}" for k, v in changed.items()), changed)
    for key, value in changed.items():
        st.save_setting(db, key, value)          # commits (the change-log row with it)
    return settings_payload()


@router.post("/backups", status_code=201)
def create_backup(db: Session = Depends(get_db)):
    info = backup_service.create_backup(db, trigger="manual")
    change_log.record(db, "backup", "create", f"Backup {info['id']} taken", {"id": info["id"], "size_bytes": info["size_bytes"]})
    db.commit()
    return info


class DeleteBackupsBody(BaseModel):
    ids: list[str] = Field(max_length=1000)


@router.delete("/backups", status_code=204)
def delete_backups(body: DeleteBackupsBody, db: Session = Depends(get_db)):
    deleted = []
    for backup_id in body.ids:
        try:
            path = backup_service.get_backup_path(backup_id)
            path.unlink()
            deleted.append(backup_id)
        except ValueError:
            pass  # skip missing IDs silently
    if deleted:
        change_log.record(db, "backup", "delete", f"Deleted {len(deleted)} backup{'s' if len(deleted) != 1 else ''}", {"ids": deleted[:50]})
        db.commit()


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


class PassphraseBody(BaseModel):
    passphrase: str = Field(min_length=backup_service.PASSPHRASE_MIN, max_length=backup_service.PASSPHRASE_MAX)


@router.post("/backups/{backup_id}/download")
def download_backup_encrypted(backup_id: str, body: PassphraseBody):
    """The backup as an encrypted copy (backup_service.encrypt_backup) under a passphrase that is
    used once here and not kept: a POST so it is never in a URL or a log. The file on the server
    stays a plain zip."""
    try:
        path = backup_service.get_backup_path(backup_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Backup not found")
    data = backup_service.encrypt_backup(path.read_bytes(), body.passphrase)
    name = backup_id + backup_service.ENCRYPTED_SUFFIX
    return Response(content=data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


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
    import card_taxonomy
    import settings_store
    card_taxonomy.refresh(db)            # the restored custom categories
    settings_store.load(db)              # the restored AI settings (secrets aren't in a backup)
    import update_check
    update_check.load(db)                # the restored update-check result and hidden release
    info = backup_service.list_backups()
    when = next((b.get("created_at") for b in info if b.get("id") == backup_id), "")
    change_log.record(db, "backup", "restore", f"Restored backup {backup_id}" + (f" from {when[:19]}" if when else ""), {"id": backup_id, "created_at": when})
    db.commit()
    return {"ok": True}


@router.post("/backups/upload", status_code=201)
async def upload_backup(file: UploadFile = File(...), passphrase: str | None = Form(None, max_length=backup_service.PASSPHRASE_MAX),
                        db: Session = Depends(get_db)):
    """A backup zip, or an encrypted copy (.flowbackup) with its passphrase, kept as an uploaded backup."""
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    if backup_service.is_encrypted(content):
        if not passphrase:
            raise HTTPException(status_code=422, detail="This backup is encrypted: enter its passphrase")
        try:
            content = backup_service.decrypt_backup(content, passphrase)
        except ValueError as exc:                 # WrongPassphrase included
            raise HTTPException(status_code=422, detail=str(exc))
    try:
        original_info = backup_service.validate_zip_bytes(content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    saved = backup_service.save_uploaded_backup(content, original_info)
    change_log.record(db, "backup", "upload", f"Uploaded backup {saved['id']}" + (" (encrypted copy)" if passphrase else ""), {"id": saved["id"], "created_at": saved.get("created_at")})
    db.commit()
    return saved
