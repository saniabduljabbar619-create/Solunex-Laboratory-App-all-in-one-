# -*- coding: utf-8 -*-
# app/services/test_request_service.py

from __future__ import annotations
from fastapi import HTTPException


from datetime import datetime, timezone # ✅ Use timezonefrom fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.core.branch_scope import resolve_branch_scope
from app.models.test_type import TestType
from app.models.test_request import TestRequest
from app.models.patient import Patient
from app.schemas.test_request import TestRequestCreate, TestRequestStatusUpdate


class TestRequestService:

    PREFIX = "REQ"
    PAD = 4
    
    def __init__(self, db: Session, current_user, requested_branch_id: int | None = None):
        self.db = db
        self.current_user = current_user
        self.branch_id = resolve_branch_scope(current_user, requested_branch_id)

    def _next_request_no(self) -> str:
        """
        Generates sequential request numbers.

        Example:
        REQ-26-0001
        REQ-26-0002
        """

        now = datetime.now()
        current_year_yy = now.strftime("%y")
        year_prefix = f"{self.PREFIX}-{current_year_yy}-"

        last = (
            self.db.query(TestRequest)
            .filter(TestRequest.request_no.like(f"{year_prefix}%"))
            .order_by(TestRequest.request_no.desc())
            .with_for_update()
            .first()
        )

        if not last or not last.request_no:
            nxt = 1
        else:
            try:
                parts = last.request_no.split("-")
                nxt = int(parts[-1]) + 1
            except (ValueError, IndexError):
                nxt = 1

        return f"{year_prefix}{nxt:0{self.PAD}d}"


    def create(self, payload: TestRequestCreate) -> TestRequest:
        effective_branch_id = payload.branch_id if hasattr(payload, 'branch_id') and payload.branch_id else self.branch_id

        if not effective_branch_id:
            raise HTTPException(status_code=400, detail="Branch context required")

        tr = TestRequest(
            sync_id=payload.sync_id, 
            request_no = self._next_request_no() if not payload.request_no else payload.request_no,
            patient_id=payload.patient_id,
            test_type_id=payload.test_type_id,
            requested_by=payload.requested_by,
            requested_note=payload.requested_note,
            status=payload.status or "pending", 
            branch_id=effective_branch_id,
        )

        self.db.add(tr)
        try:
            self.db.commit()
            self.db.refresh(tr)
            return tr
        except Exception as e:
            self.db.rollback()
            print(f"DEBUG: Error creating test request: {str(e)}")
            raise HTTPException(status_code=400, detail="Database integrity error")

    def list(
        self,
        status: str | None = None,
        patient_id: int | None = None,
        created_date: str | None = None,
        limit: int = 50
    ):
        # Join all three tables to get the full picture
        q = (
            self.db.query(TestRequest, TestType, Patient)
            .join(TestType, TestType.id == TestRequest.test_type_id)
            .join(Patient, Patient.id == TestRequest.patient_id)
        )

        if self.branch_id:
            q = q.filter(TestRequest.branch_id == self.branch_id)

        if status:
            q = q.filter(TestRequest.status == status)

        if patient_id:
            q = q.filter(TestRequest.patient_id == patient_id)

        if created_date:
            q = q.filter(func.date(TestRequest.created_at) == created_date)

        rows = q.order_by(TestRequest.created_at.desc()).limit(limit).all()

        out = []

        for tr, tt, p in rows: 
            # Ensure the ISO format includes the UTC offset (+00:00)
            # If tr.created_at is aware, isoformat() handles this automatically.
            iso_date = tr.created_at.isoformat() if tr.created_at else None
            
            out.append({
                "id": tr.id,
                "request_no": tr.request_no,
                "sync_id": tr.sync_id,
                "patient_id": tr.patient_id,
                "test_type_id": tr.test_type_id,
                "status": tr.status,
                "requested_by": tr.requested_by,
                
                "created_at": iso_date,
                "date": iso_date, # Fallback for frontend compatibility
                "updated_at": tr.updated_at.isoformat() if tr.updated_at else None,
                
                "patient": {
                    "id": p.id,
                    "patient_no": p.patient_no,
                    "full_name": p.full_name,
                    "phone": p.phone,
                    "gender": p.gender,
                    "age_value": p.age_value,
                    "age_unit": p.age_unit,
                    "dob": p.date_of_birth.isoformat() if p.date_of_birth else None,
                    "referrer_id": p.referrer_id,
                    "created_at": iso_date,
                    "date": iso_date,
                },

                "test_name": tt.name,
                "price": float(tt.price) if tt.price else 0.0,
            })

        return out

    def get(self, request_id: int) -> TestRequest:
        q = self.db.query(TestRequest).filter(TestRequest.id == request_id)
        if self.branch_id:
            q = q.filter(TestRequest.branch_id == self.branch_id)

        tr = q.first()
        if not tr:
            raise HTTPException(status_code=404, detail="Test request not found")
        return tr


    def update_status(self, request_id: int, payload: TestRequestStatusUpdate) -> TestRequest:
        tr = self.get(request_id)

        allowed = {
            "pending": {"paid", "rejected"},
            "paid": {"accepted", "rejected"},
            "accepted": {"fulfilled", "in_progress"}, 
            "rejected": set(),
            "fulfilled": set(),
        }

        if payload.status != tr.status and payload.status not in allowed.get(tr.status, set()):
            raise HTTPException(status_code=400, detail=f"Invalid transition {tr.status} -> {payload.status}")

        tr.status = payload.status

        if payload.status == "accepted" and tr.accepted_at is None:
                    # ✅ Proper UTC aware datetime
                    tr.accepted_at = datetime.now(timezone.utc)

        if payload.status == "fulfilled" and tr.fulfilled_at is None:
            # ✅ Proper UTC aware datetime
            tr.fulfilled_at = datetime.now(timezone.utc)

        if payload.test_result_id:
            tr.test_result_id = payload.test_result_id

        self.db.commit()
        self.db.refresh(tr)
        return tr