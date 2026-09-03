# -*- coding: utf-8 -*-
# app/api/routers/results.py
from __future__ import annotations

from typing import Optional

from fastapi.responses import FileResponse
from fastapi import HTTPException
from app.services.result_pdf_service import generate_result_pdf
from app.models.test_result import ResultStatus

from fastapi import APIRouter, Depends, Header, Query, BackgroundTasks
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.dependencies import get_current_user  # ← REQUIRED

from app.schemas.test_result import (
    ResultInstantiate,
    ResultUpdateValues,
    ResultSetStatus,
    TestResultOut,
    PagedTestResultOut,
    ResultInstantiateFromSnapshot,
)

from app.services.result_service import ResultService
from app.services.ssdo.tasks import index_result_task
from app.services.sas.tasks import generate_predictions_task
from app.services.sas.announcer_tasks import announce_result_by_severity_task
from pydantic import BaseModel


router = APIRouter()


class BatchReleaseIn(BaseModel):
    result_ids: list[int]
    

@router.post("/instantiate", response_model=TestResultOut)
def instantiate_result(
    payload: ResultInstantiate,
    background_tasks: BackgroundTasks,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = ResultService(db, current_user)
    result = service.instantiate(payload)

    # v2.0 — SSDO indexes the new draft, SAS predicts likely values
    # from the patient's prior history, before the scientist starts typing.
    background_tasks.add_task(index_result_task, result.id)
    background_tasks.add_task(generate_predictions_task, result.id)
    background_tasks.add_task(announce_result_by_severity_task, result.id, result.branch_id)

    return result


@router.post("/from-snapshot", response_model=TestResultOut)
def instantiate_from_snapshot(
    payload: ResultInstantiateFromSnapshot,
    background_tasks: BackgroundTasks,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = ResultService(db, current_user)
    result = service.instantiate_from_snapshot(payload)

    background_tasks.add_task(index_result_task, result.id)
    background_tasks.add_task(generate_predictions_task, result.id)
    background_tasks.add_task(announce_result_by_severity_task, result.id, result.branch_id)

    return result

@router.post("/bundle-report")
def bundle_report(
    payload: dict,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Render multiple results for the SAME patient into one combined PDF."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    from app.models.test_result import TestResult
    from app.services.result_pdf_service import generate_bundle_pdf

    result_ids = payload.get("result_ids", [])
    if not result_ids:
        raise HTTPException(status_code=400, detail="No results selected.")

    results = db.query(TestResult).filter(TestResult.id.in_(result_ids)).all()
    if not results:
        raise HTTPException(status_code=404, detail="Results not found.")

    pids = {r.patient_id for r in results}
    if len(pids) > 1:
        raise HTTPException(status_code=400, detail="All results must belong to the same patient.")

    pdf_path = generate_bundle_pdf(results, source="lab")
    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=pdf_path.name,
    )


@router.get("/{result_id}", response_model=TestResultOut)
def get_result(
    result_id: int,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = ResultService(db, current_user)
    return service.get(result_id)


@router.patch("/{result_id}/values", response_model=TestResultOut)
def update_result_values(
    result_id: int,
    payload: ResultUpdateValues,
    background_tasks: BackgroundTasks,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = ResultService(db, current_user)
    result = service.update_values(result_id, payload)

    # v2.0 — values (and therefore flags) just changed, re-index immediately.
    background_tasks.add_task(index_result_task, result.id)

    return result


@router.patch("/{result_id}/status", response_model=TestResultOut)
def set_result_status(
    result_id: int,
    payload: ResultSetStatus,
    background_tasks: BackgroundTasks,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
    x_role: str = Header(default="labtech"),
):
    service = ResultService(db, current_user)
    result = service.set_status(result_id, payload.status, role=x_role)

    # v2.0 — status change affects portal_visible (released = visible),
    # re-index so SSDO reflects the new visibility state.
    background_tasks.add_task(index_result_task, result.id)

    return result


@router.get("", response_model=PagedTestResultOut)
def list_results(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
    patient_id: Optional[int] = Query(default=None),
    status: Optional[str] = Query(default=None),
    created_date: Optional[str] = Query(default=None),
    order: Optional[str] = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    x_role: str = Header(default="labtech"),
):
    service = ResultService(db, current_user)
    rows, total = service.list(
        patient_id=patient_id,
        status=status,
        created_date=created_date,
        order=order,
        limit=limit,
        offset=offset,
        role=x_role,
    )

    # Enrich each result with patient + test-name for list consumers (bell, tabs)
    from app.models.patient import Patient
    from app.models.test_type import TestType
    out = []
    for r in rows:
        item = TestResultOut.model_validate(r).model_dump()
        p = db.query(Patient).filter(Patient.id == r.patient_id).first()
        tt = db.query(TestType).filter(TestType.id == r.test_type_id).first()
        item["patient_info"] = (
            {"id": p.id, "full_name": p.full_name, "patient_no": p.patient_no, "phone": p.phone}
            if p else None
        )
        item["test_name"] = tt.name if tt else None
        out.append(item)

    return {"value": out, "Count": total}


@router.get("/{result_id}/reprint")
def reprint_result(
    result_id: int,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = ResultService(db, current_user)
    result = service.get(result_id)

    allowed_statuses = [ResultStatus.released, ResultStatus.draft]

    if result.status not in allowed_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Result in '{result.status}' status cannot be printed. Must be Draft or Released."
        )

    output_path = generate_result_pdf(result, source="lab")

    return FileResponse(
        path=output_path,
        media_type="application/pdf",
        filename=f"result_{result.id}.pdf",
    )
    
    
@router.post("/release-batch")
def release_batch(
    payload: BatchReleaseIn,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
    x_role: str = Header(default="admin"),
):
    """
    Release multiple results for one patient in a single operation,
    sending ONE combined SMS for the whole batch (cost-efficient).
    Used by the cashier's unify-and-release action.
    """
    service = ResultService(db, current_user)
    return service.release_batch(payload.result_ids, role=x_role)