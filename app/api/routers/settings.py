# -*- coding: utf-8 -*-
# app/api/routers/settings.py
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.api.deps import get_db
from app.core.dependencies import get_current_user
from app.services.numbering_service import NumberingService

router = APIRouter(prefix="/api/settings", tags=["settings"])


class NumberingConfig(BaseModel):
    patient_number_format: str | None = None
    patient_reset_policy: str | None = None
    lab_number_format: str | None = None
    lab_reset_policy: str | None = None


@router.get("/numbering")
def get_numbering(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    svc = NumberingService(db)
    cfg = svc.get_all_config()
    return {
        **cfg,
        "patient_preview": svc.preview(cfg["patient_number_format"]),
        "lab_preview": svc.preview(cfg["lab_number_format"]),
    }


@router.post("/numbering")
def save_numbering(payload: NumberingConfig, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    svc = NumberingService(db)
    # validate formats
    if payload.patient_number_format:
        ok, msg = svc.validate(payload.patient_number_format)
        if not ok:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail=f"Patient format: {msg}")
    if payload.lab_number_format:
        ok, msg = svc.validate(payload.lab_number_format)
        if not ok:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail=f"Lab format: {msg}")
    cfg = svc.save_config(payload.model_dump(exclude_none=True))
    return {
        **cfg,
        "patient_preview": svc.preview(cfg["patient_number_format"]),
        "lab_preview": svc.preview(cfg["lab_number_format"]),
    }


@router.get("/numbering/preview")
def preview_format(fmt: str, db: Session = Depends(get_db)):
    svc = NumberingService(db)
    ok, msg = svc.validate(fmt)
    return {"valid": ok, "message": msg, "preview": svc.preview(fmt) if ok else None}


class SetNextSequence(BaseModel):
    kind: str   # "patient" | "lab"
    next_value: int


@router.post("/numbering/set-next")
def set_next_sequence(payload: SetNextSequence, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    from fastapi import HTTPException
    svc = NumberingService(db)
    if payload.kind not in ("patient", "lab"):
        raise HTTPException(status_code=400, detail="kind must be 'patient' or 'lab'")
    if payload.next_value < 1:
        raise HTTPException(status_code=400, detail="next_value must be at least 1")
    try:
        return svc.set_next_sequence(payload.kind, payload.next_value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ══════════════════════════════════════════════════════════════════
# Report Settings — appearance / content toggles for lab result PDFs.
# Stored as a single JSON blob in SystemConfig under key "report_settings".
# Read/written by the desktop Lab Settings screen. The result PDF renderer
# can read these later to honour the toggles at print time.
# ══════════════════════════════════════════════════════════════════
import json as _json
from app.models.system_config import SystemConfig

_REPORT_SETTINGS_KEY = "report_settings"

# Canonical defaults — the shape the desktop expects back on first run.
REPORT_SETTINGS_DEFAULTS = {
    # Appearance
    "show_logo": True,
    "show_qr": True,
    "show_ref_ranges": True,
    "show_flag_column": True,
    "show_sas_trend_footnote": True,
    # Content
    "show_interpretation": True,
    "show_analyst_name": True,
    "footer_note": "This result is validated by a certified laboratory scientist.",
}


class ReportSettingsIn(BaseModel):
    show_logo: bool | None = None
    show_qr: bool | None = None
    show_ref_ranges: bool | None = None
    show_flag_column: bool | None = None
    show_sas_trend_footnote: bool | None = None
    show_interpretation: bool | None = None
    show_analyst_name: bool | None = None
    footer_note: str | None = None


def _load_report_settings(db: Session) -> dict:
    row = db.query(SystemConfig).filter(SystemConfig.key == _REPORT_SETTINGS_KEY).first()
    merged = dict(REPORT_SETTINGS_DEFAULTS)
    if row and row.value:
        try:
            stored = _json.loads(row.value)
            if isinstance(stored, dict):
                merged.update(stored)
        except Exception:
            pass
    return merged


def _save_report_settings(db: Session, data: dict) -> dict:
    current = _load_report_settings(db)
    # Only overwrite keys that were actually provided (exclude_none upstream).
    for k, v in data.items():
        if k in REPORT_SETTINGS_DEFAULTS and v is not None:
            current[k] = v
    row = db.query(SystemConfig).filter(SystemConfig.key == _REPORT_SETTINGS_KEY).first()
    payload = _json.dumps(current)
    if row:
        row.value = payload
    else:
        row = SystemConfig(key=_REPORT_SETTINGS_KEY, value=payload)
        db.add(row)
    db.commit()
    return current


@router.get("/report")
def get_report_settings(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Return the lab's report appearance/content settings (with defaults filled in)."""
    return _load_report_settings(db)


@router.post("/report")
def save_report_settings(
    payload: ReportSettingsIn,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Persist report appearance/content toggles. Partial updates supported."""
    saved = _save_report_settings(db, payload.model_dump(exclude_none=True))
    return saved