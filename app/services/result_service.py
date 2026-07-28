# -*- coding: utf-8 -*-
# app/services/result_service.py
from __future__ import annotations

from typing import Optional, Tuple, List
from datetime import datetime, timezone, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc, asc, func

from app.models.patient import Patient
from app.models.test_template import TestTemplate
from app.models.test_result import TestResult, ResultStatus
from app.models.test_request import TestRequest  # ✅ REQUIRED FOR TIMELINE
from app.schemas.test_result import ResultInstantiate, ResultUpdateValues, ResultInstantiateFromSnapshot
from app.services.compute_service import ComputeService
from app.services.audit_service import AuditService
from app.core.branch_scope import resolve_branch_scope
from app.services.notification_service import NotificationService

class ResultService:
    def __init__(self, db: Session, current_user, requested_branch_id: int | None = None):
        self.db = db
        self.current_user = current_user
        self.branch_id = resolve_branch_scope(current_user, requested_branch_id)

    def instantiate(self, payload: ResultInstantiate) -> TestResult:
        if not self.branch_id:
            raise HTTPException(status_code=400, detail="User not bound to branch")
        
        patient = self.db.query(Patient).filter(
            Patient.id == payload.patient_id,
            Patient.branch_id == self.branch_id
        ).first()

        if not patient:
            raise HTTPException(status_code=404, detail="Patient not found")

        template = self.db.query(TestTemplate).filter(TestTemplate.id == payload.template_id).first()
        if not template or not template.is_active:
            raise HTTPException(status_code=400, detail="Invalid or inactive template")

        r = TestResult(
            patient_id=patient.id,
            test_type_id=template.test_type_id,
            template_id=template.id,
            status=ResultStatus.draft,
            template_snapshot=template.structure,
            values={},
            flags={},
            notes=None,
            branch_id=self.branch_id,
        )
        self.db.add(r)
        self.db.commit()
        self.db.refresh(r)
        return r
    
    def instantiate_from_snapshot(self, payload: ResultInstantiateFromSnapshot) -> TestResult:
        effective_branch_id = payload.branch_id if hasattr(payload, 'branch_id') and payload.branch_id else self.branch_id

        if not effective_branch_id:
            raise HTTPException(status_code=400, detail="User not bound to branch")
        
        patient = self.db.query(Patient).filter(
            Patient.id == payload.patient_id,
            Patient.branch_id == effective_branch_id
        ).first()

        if not patient:
            raise HTTPException(status_code=404, detail="Patient not found")

        snapshot = payload.template_snapshot if isinstance(payload.template_snapshot, dict) else {}
        if not snapshot:
            raise HTTPException(status_code=400, detail="template_snapshot is required")

        values = payload.values if isinstance(payload.values, dict) else {}
        flags = ComputeService.compute_flags(snapshot, values)

        r = TestResult(
            sync_id=payload.sync_id,
            patient_id=patient.id,
            test_type_id=payload.test_type_id,
            template_id=payload.template_id,
            status=payload.status or ResultStatus.draft,
            template_snapshot=snapshot,
            values=values,
            flags=flags,
            notes=payload.notes,
            branch_id=effective_branch_id,
        )

        self.db.add(r)
        self.db.flush() # ✅ Get result ID to link back to request

        # -------------------------------------------------
        # 🔗 CLINICAL TIMELINE HANDSHAKE
        # -------------------------------------------------
        if hasattr(payload, 'test_request_id') and payload.test_request_id:
            req = self.db.query(TestRequest).filter(TestRequest.id == payload.test_request_id).first()
            if req:
                req.test_result_id = r.id
                req.status = "fulfilled"
                # This fills the "Released" date on the PDF
                req.fulfilled_at = func.now() 

        self.db.commit()
        self.db.refresh(r)

        AuditService(self.db).log(
            actor_type="system" if payload.sync_id else "staff",
            actor="sync_engine" if payload.sync_id else "labtech",
            action="instantiate",
            entity="test_result",
            entity_id=r.id,
            meta={"source": "snapshot", "status": r.status.value},
        )

        return r

    def get(self, result_id: int) -> TestResult:
        r = self.db.query(TestResult).filter(TestResult.id == result_id).first()
        if not r:
            raise HTTPException(status_code=404, detail="Result not found")
        return r

    def update_values(self, result_id: int, payload: ResultUpdateValues) -> TestResult:
        r = self.get(result_id)
        existing = r.values if isinstance(r.values, dict) else {}
        incoming = payload.values if isinstance(payload.values, dict) else {}
        merged = {**existing, **incoming}

        r.values = merged
        r.flags = ComputeService.compute_flags(r.template_snapshot, merged)

        if payload.notes is not None:
            r.notes = payload.notes

        self.db.commit()
        self.db.refresh(r)
        return r

    def set_status(self, result_id: int, new_status: str, role: str) -> TestResult:
        r = self.get(result_id)
        role = (role or "").lower().strip()
        new_status = (new_status or "").lower().strip()

        try:
            new_enum = ResultStatus(new_status)
        except Exception:
            raise HTTPException(status_code=400, detail=f"Invalid status: {new_status}")

        allowed = {
            "labtech": {"draft": {"in_progress"}, "in_progress": {"pending_review"}},
            "supervisor": {"pending_review": {"approved"}, "approved": {"released"}},
            "admin": {s: {"released"} for s in ["draft", "in_progress", "pending_review", "approved"]},
        }

        if role not in allowed:
            raise HTTPException(status_code=403, detail="Invalid role")

        current = r.status.value if hasattr(r.status, "value") else str(r.status)
        next_allowed = allowed[role].get(current, set())

        if new_status not in next_allowed:
            raise HTTPException(
                status_code=400,
                detail=f"Transition not allowed: {current} -> {new_status} for role {role}",
            )

        r.status = new_enum

        # 🚀 UPDATE REQUEST TIMELINE ON RELEASE
        if new_status == "released":
            req = self.db.query(TestRequest).filter(TestRequest.test_result_id == r.id).first()
            if req:
                req.status = "fulfilled"
                if not req.fulfilled_at:
                    req.fulfilled_at = func.now()

        self.db.commit()
        self.db.refresh(r)

        # Audit log
        AuditService(self.db).log(
            actor_type="staff", actor=role, action="status_change",
            entity="test_result", entity_id=r.id,
            meta={"from": current, "to": new_status},
        )

        if new_status == "released":
            patient = self.db.query(Patient).filter(Patient.id == r.patient_id).first()
            patient_name = patient.full_name if patient else "Patient"
            phone = patient.phone if patient else None

            NotificationService.create(
                db=self.db, type="result_ready", title="Result Ready",
                message=f"Lab result ready for {patient_name}",
                reference_type="test_result", reference_id=r.id
            )

            if phone:
                try:
                    from app.services.barcode_service import get_portal_qr_url
                    suffix = (patient.patient_no or "").split("-")[-1] if patient and patient.patient_no else ""
                    login_url = get_portal_qr_url(phone=phone, patient_no=patient.patient_no if patient else "")
                    sms_message = (
                        f"Dear {patient_name}, your test result is ready.\n"
                        f"Login to view/download:\n"
                        f"Username: {phone}\n"
                        f"Password: {suffix}\n"
                        f"{login_url}"
                    )
                    NotificationService.send_sms(phone=phone, message=sms_message)
                except Exception as sms_error:
                    print(f"[SMS ERROR] {sms_error}")

        return r

    def list(self, patient_id=None, status=None, created_date=None, order="desc", limit=50, offset=0, role="labtech") -> tuple[list[TestResult], int]:
        role = (role or "").lower().strip()
        if role not in {"labtech", "labstaff", "cashier", "supervisor", "admin"}:
            raise HTTPException(status_code=403, detail="Invalid role")

        q = self.db.query(TestResult)
        if self.branch_id:
            q = q.filter(TestResult.branch_id == self.branch_id)
        if patient_id is not None:
            q = q.filter(TestResult.patient_id == patient_id)
        if status:
            q = q.filter(TestResult.status == ResultStatus(status.lower().strip()))
        if created_date:
            try:
                day = datetime.strptime(created_date, "%Y-%m-%d")
                tz_offset = timezone(timedelta(hours=1))  # Nigeria UTC+1, matches PatientService.search()

                start_local = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz_offset)
                end_local = start_local + timedelta(days=1)

                start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
                end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)

                q = q.filter(TestResult.created_at >= start_utc, TestResult.created_at < end_utc)
            except ValueError:
                pass  # bad date string → ignore filter rather than error the whole list

        total = q.count()
        # Default stays "desc" (unchanged) — history_tab.py, result_tab.py, and Lab's
        # review/editor views all rely on the existing most-recent-first behavior.
        # order="asc" is opt-in, used by the Cashier notification bell for FIFO.
        order_fn = asc if (order or "").lower() == "asc" else desc
        rows = q.order_by(order_fn(TestResult.created_at)).offset(offset).limit(limit).all()
        return rows, total
    
    
    def release_batch(self, result_ids: list[int], role: str) -> dict:
        """
        Releases multiple results for the SAME patient in one operation
        and sends ONE combined SMS for the whole batch (cost-efficient).
        Cashier/admin use this from the Result tab's unify-and-release.
        """
        role = (role or "").lower().strip()
        if role not in ("admin", "supervisor"):
            # cashier releases via admin-level authority in this flow
            role = "admin"

        if not result_ids:
            raise HTTPException(status_code=400, detail="No results provided.")

        released = []
        patient = None

        for rid in result_ids:
            r = self.get(rid)
            current = r.status.value if hasattr(r.status, "value") else str(r.status)
            if current == "released":
                released.append(r)
                continue
            # Force to released (admin authority)
            r.status = ResultStatus.released
            # Flip linked request to fulfilled
            req = self.db.query(TestRequest).filter(TestRequest.test_result_id == r.id).first()
            if req:
                req.status = "fulfilled"
                if not req.fulfilled_at:
                    req.fulfilled_at = func.now()
            released.append(r)
            if patient is None:
                patient = self.db.query(Patient).filter(Patient.id == r.patient_id).first()

        self.db.commit()
        for r in released:
            self.db.refresh(r)

        # Audit
        AuditService(self.db).log(
            actor_type="staff", actor=role, action="batch_release",
            entity="test_result", entity_id=None,
            meta={"result_ids": result_ids, "count": len(released)},
        )

        # ONE combined SMS for the whole batch
        sms_sent = False
        if patient and patient.phone:
            count = len(released)
            patient_name = patient.full_name
            try:
                NotificationService.create(
                    db=self.db, type="result_ready", title="Results Ready",
                    message=f"{count} result(s) ready for {patient_name}",
                    reference_type="patient", reference_id=patient.id,
                )
                suffix = (patient.patient_no or "").split("-")[-1] if patient.patient_no else ""
                from app.services.barcode_service import get_portal_qr_url
                login_url = get_portal_qr_url(phone=patient.phone, patient_no=patient.patient_no or "")
                sms_message = (
                    f"Dear {patient_name}, your result{'s' if count != 1 else ''} "
                    f"for {count} test{'s' if count != 1 else ''} {'are' if count != 1 else 'is'} ready.\n"
                    f"Login to view/download:\n"
                    f"Username: {patient.phone}\n"
                    f"Password: {suffix}\n"
                    f"{login_url}"
                )
                NotificationService.send_sms(phone=patient.phone, message=sms_message)
                sms_sent = True
            except Exception as sms_error:
                print(f"[SMS ERROR] batch release: {sms_error}")

        return {
            "released_count": len(released),
            "result_ids": [r.id for r in released],
            "sms_sent": sms_sent,
        }