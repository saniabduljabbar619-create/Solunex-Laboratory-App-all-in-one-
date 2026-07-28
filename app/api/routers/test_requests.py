# -*- coding: utf-8 -*-
# app/api/routers/test_requests.py

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from typing import Optional

from app.api.deps import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.models.test_request import TestRequest

from app.schemas.test_request import (
    TestRequestCreate,
    TestRequestOut,
    TestRequestStatusUpdate,
)
from app.services.test_request_service import TestRequestService

from pydantic import BaseModel
from app.services.numbering_service import NumberingService

router = APIRouter()

class BatchTestRequestIn(BaseModel):
    patient_id: int
    test_type_ids: list[int]
    requested_by: str | None = None




@router.post("", response_model=TestRequestOut)
def create_test_request(
    payload: TestRequestCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = TestRequestService(db, current_user)
    return service.create(payload)


@router.get("")
def list_test_requests(
    status: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    patient_id: Optional[int] = Query(default=None),
    # 🔥 ADDED: Filter by date (YYYY-MM-DD)
    created_date: Optional[str] = Query(default=None), 
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = TestRequestService(db, current_user)
    effective_status = status or q
    
    return service.list(
        status=effective_status,
        patient_id=patient_id,
        created_date=created_date, # Pass it to the service
        limit=limit,
    )

@router.patch("/{request_id}/status", response_model=TestRequestOut)
def update_test_request_status(
    request_id: int,
    payload: TestRequestStatusUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = TestRequestService(db, current_user)
    return service.update_status(request_id, payload)


@router.get("/count")
def get_test_requests_count(
    status: str = "paid",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = TestRequestService(db, current_user)
    
    # Query directly for the count, respecting the user's branch
    count = db.query(TestRequest).filter(
        TestRequest.status == status,
        TestRequest.branch_id == service.branch_id
    ).count()
    
    return {"count": count}


@router.post("/batch")
def create_batch(
    payload: BatchTestRequestIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Create multiple test requests as ONE visit batch sharing a single
    Lab Number. Generates the lab number once and stamps all requests.
    """
    if not payload.test_type_ids:
        raise HTTPException(status_code=400, detail="No tests provided.")

    branch_id = getattr(current_user, "branch_id", None)
    lab_number = NumberingService(db).next_lab_number()
    # Stamp the actual cashier's name for accountability — never generic "Cashier"
    cashier_name = payload.requested_by or getattr(current_user, "username", None) or "Unknown"

    created = []
    for tt_id in payload.test_type_ids:
        req = TestRequest(
            patient_id=payload.patient_id,
            test_type_id=tt_id,
            requested_by=cashier_name,
            status="pending",
            lab_number=lab_number,
            branch_id=branch_id,
        )
        db.add(req)
        created.append(req)
    db.commit()
    for r in created:
        db.refresh(r)

    return {
        "lab_number": lab_number,
        "count": len(created),
        "request_ids": [r.id for r in created],
    }
    
    
@router.get("/lab-queue")
def lab_queue(
    scope: str = Query(default="today"),   # "today" | "late"
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Returns patients who have PAID test requests awaiting lab work,
    grouped by patient with their queued test count, highest priority,
    and earliest request time. Ordered by priority tier (emergency first),
    then newest-request-first (LIFO) within each tier.

    scope="today" (default): only requests created today — this is the
    working queue a lab scientist actually acts on day to day.
    scope="late": everything created before today — the backlog, requested
    explicitly. Also always returns late_count regardless of scope, so a
    lab scientist can see backlog exists even while viewing today's queue —
    deliberately not hiding it silently, since an old paid-but-unprocessed
    request may be a real specimen still waiting, not just stale data.
    """
    from datetime import datetime, timezone, timedelta
    from app.models.patient import Patient
    from app.services.test_request_service import TestRequestService

    svc = TestRequestService(db, current_user)
    branch_id = svc.branch_id

    tz_offset = timezone(timedelta(hours=1))  # Nigeria UTC+1, matches Patient/Result services
    now_local = datetime.now(tz_offset)
    today_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end_local = today_start_local + timedelta(days=1)
    today_start_utc = today_start_local.astimezone(timezone.utc).replace(tzinfo=None)
    today_end_utc = today_end_local.astimezone(timezone.utc).replace(tzinfo=None)

    base_q = db.query(TestRequest).filter(TestRequest.status == "paid")
    if branch_id:
        base_q = base_q.filter(TestRequest.branch_id == branch_id)

    if scope == "late":
        q = base_q.filter(TestRequest.created_at < today_start_utc)
    else:
        q = base_q.filter(TestRequest.created_at >= today_start_utc, TestRequest.created_at < today_end_utc)

    requests = q.order_by(TestRequest.created_at.asc()).all()

    # Always computed, regardless of scope — feeds the "Late Requests (N)"
    # button's badge so backlog is never invisible.
    late_count = base_q.filter(TestRequest.created_at < today_start_utc).count()

    # Group by patient
    priority_rank = {"emergency": 0, "urgent": 1, "active": 2, "normal": 3}
    grouped: dict[int, dict] = {}
    for r in requests:
        pid = r.patient_id
        if pid not in grouped:
            grouped[pid] = {
                "patient_id": pid,
                "queued_count": 0,
                "priority": "normal",
                "earliest": r.created_at,
                "lab_number": r.lab_number,
            }
        g = grouped[pid]
        g["queued_count"] += 1
        # keep highest priority
        if priority_rank.get(r.priority, 3) < priority_rank.get(g["priority"], 3):
            g["priority"] = r.priority
        if r.created_at and (not g["earliest"] or r.created_at < g["earliest"]):
            g["earliest"] = r.created_at

    # Attach patient info
    patient_ids = list(grouped.keys())
    patients = db.query(Patient).filter(Patient.id.in_(patient_ids)).all() if patient_ids else []
    pmap = {p.id: p for p in patients}

    result = []
    for pid, g in grouped.items():
        p = pmap.get(pid)
        if not p:
            continue
        result.append({
            **g,
            "earliest": g["earliest"].isoformat() if g["earliest"] else None,
            "full_name": p.full_name,
            "patient_no": p.patient_no,
            "gender": p.gender,
            "age_value": p.age_value,
            "age_unit": p.age_unit,
            "referrer_id": p.referrer_id,
            "phone": p.phone,
        })

    # Priority still escalates emergencies/urgent to the top. Within the same
    # priority tier, newest request first (LIFO) — a stable two-pass sort:
    # sort by timestamp descending first, then stable-sort by priority so the
    # LIFO ordering within each tier survives the second pass.
    result.sort(key=lambda x: x["earliest"] or "", reverse=True)
    result.sort(key=lambda x: priority_rank.get(x["priority"], 3))
    return {"patients": result, "late_count": late_count, "scope": scope}



class PriorityUpdate(BaseModel):
    patient_id: int
    priority: str

@router.post("/set-priority")
def set_priority(
    payload: PriorityUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set priority on all a patient's queued (paid) requests."""
    if payload.priority not in ("normal", "active", "urgent", "emergency"):
        raise HTTPException(status_code=400, detail="Invalid priority.")
    reqs = db.query(TestRequest).filter(
        TestRequest.patient_id == payload.patient_id,
        TestRequest.status == "paid",
    ).all()
    for r in reqs:
        r.priority = payload.priority
    db.commit()
    return {"updated": len(reqs), "priority": payload.priority}