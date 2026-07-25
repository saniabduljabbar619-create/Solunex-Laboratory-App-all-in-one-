# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_, func, asc, desc
from sqlalchemy.exc import IntegrityError

from app.models.patient import Patient
from app.schemas.patient import PatientCreate, PatientUpdate
from app.core.branch_scope import resolve_branch_scope

import secrets
import string
from app.core.security import hash_password


class PatientService:
    # Enterprise Format: IEL-YY-NNNN
    PREFIX = "IEL"
    PAD = 4  

    def __init__(self, db: Session, current_user, requested_branch_id: int | None = None):
        self.db = db
        self.current_user = current_user
        self.branch_id = resolve_branch_scope(current_user, requested_branch_id)

    def _next_patient_no(self) -> str:
        """
        Generates the next sequential Lab ID.
        Flow:
        - First 2026 entry: IEL-26-3150
        - Next: IEL-26-3151
        - Jan 1 2027: IEL-27-0001
        """
        # 1. Get current year suffix (e.g., '26')
        now = datetime.now()
        current_year_yy = now.strftime("%y") 
        year_prefix = f"{self.PREFIX}-{current_year_yy}-"

        # 2. Find the highest number for the current year prefix
        # We use 'with_for_update' to prevent race conditions during high-volume registration
        last = (
            self.db.query(Patient)
            .filter(Patient.patient_no.like(f"{year_prefix}%"))
            .order_by(Patient.patient_no.desc())
            .with_for_update()
            .first()
        )

        if not last:
            # Check if we are starting the 2026 sequence
            if current_year_yy == "26":
                nxt = 4268
            else:
                # Standard New Year reset
                nxt = 1
        else:
            # Parse the numeric tail after the last dash
            try:
                parts = last.patient_no.split("-")
                nxt = int(parts[-1]) + 1
            except (ValueError, IndexError):
                nxt = 1

        return f"{year_prefix}{nxt:0{self.PAD}d}"
    
    
    def _generate_portal_code(self, length: int = 6) -> str:
        """
        Generates a clean, unambiguous alphanumeric portal code.
        Excludes look-alike characters (0/O, 1/l/I) so patients can
        read it correctly off an SMS.
        """
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ" + "23456789"
        return "".join(secrets.choice(alphabet) for _ in range(length))
    

    def create(self, payload: PatientCreate) -> Patient:
        data = payload.model_dump()

        # 1. Patient number generation
        patient_no = (data.get("patient_no") or "").strip()
        if not patient_no:
            patient_no = self._next_patient_no()
            data["patient_no"] = patient_no

        # 2. Branch resolution
        if data.get("branch_id") is None:
            data["branch_id"] = self.branch_id

        # 3. Duplicate check
        exists = self.db.query(Patient).filter(Patient.patient_no == patient_no).first()
        if exists:
            raise HTTPException(status_code=400, detail="Patient number already exists")

        # 4. Portal credential — generate if portal enabled
        portal_code_plain = None
        if data.get("portal_enabled", True):
            portal_code_plain = self._generate_portal_code()
            data["portal_code"] = hash_password(portal_code_plain)
        else:
            data["portal_code"] = None

        try:
            p = Patient(**data)
            self.db.add(p)
            self.db.commit()
            self.db.refresh(p)
            # Attach the plaintext code ONCE for the response — not persisted.
            p.portal_code_plain = portal_code_plain
            return p
        except IntegrityError as e:
            self.db.rollback()
            print(f"DEBUG: IntegrityError during create: {str(e)}")
            raise HTTPException(status_code=400, detail="Database integrity error")

    def get(self, patient_id: int) -> Patient:
        query = self.db.query(Patient).filter(Patient.id == patient_id)
        if self.branch_id:
            query = query.filter(Patient.branch_id == self.branch_id)
        
        p = query.first()
        if not p:
            raise HTTPException(status_code=404, detail="Patient not found")
        return p

    def update(self, patient_id: int, payload: PatientUpdate) -> Patient:
        p = self.get(patient_id)
        data = payload.model_dump(exclude_unset=True)
        for k, v in data.items():
            setattr(p, k, v)
        self.db.commit()
        self.db.refresh(p)
        return p

    def search(self, q: str | None = None, created_date: str | None = None, order: str = "desc") -> list[Patient]:
        """
        Search by query string (Name, Phone, ID) or filter by specific date.

        `order` only affects the plain search / "all patients" paths below —
        the created_date (queue) path always returns FIFO (oldest-first),
        which was already its behavior and is left unchanged. Default "desc"
        preserves existing behavior for Lab and Blood Bank callers; Cashier
        opts into "asc" (FIFO) explicitly.
        """
        query = self.db.query(Patient)
        order_fn = asc if (order or "").lower() == "asc" else desc

        if self.branch_id:
            query = query.filter(Patient.branch_id == self.branch_id)

        if q and q.strip():
            search_str = q.strip()
            return (
                query.filter(
                    or_(
                        Patient.full_name.ilike(f"%{search_str}%"),
                        Patient.phone.ilike(f"%{search_str}%"),
                        Patient.patient_no.ilike(f"%{search_str}%"),
                    )
                )
                .order_by(order_fn(Patient.id))
                .limit(50)
                .all()
            )

        if created_date:
            try:
                day = datetime.strptime(created_date, "%Y-%m-%d")
                tz_offset = timezone(timedelta(hours=1)) # Nigeria UTC+1

                start_local = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz_offset)
                end_local = start_local + timedelta(days=1)

                # Convert to naive UTC for DB comparison
                start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
                end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)

                # Today's queue = patients REGISTERED today OR with a REQUEST today
                from app.models.test_request import TestRequest

                reg_q = self.db.query(Patient.id).filter(
                    Patient.created_at >= start_utc,
                    Patient.created_at < end_utc,
                )
                if self.branch_id:
                    reg_q = reg_q.filter(Patient.branch_id == self.branch_id)

                req_q = self.db.query(TestRequest.patient_id).filter(
                    TestRequest.created_at >= start_utc,
                    TestRequest.created_at < end_utc,
                )
                if self.branch_id:
                    req_q = req_q.filter(TestRequest.branch_id == self.branch_id)

                patient_ids = {r[0] for r in reg_q.all()} | {r[0] for r in req_q.all() if r[0]}
                if not patient_ids:
                    return []

                return (
                    query.filter(Patient.id.in_(patient_ids))
                    .order_by(order_fn(Patient.created_at))
                    .all()
                )
            except ValueError:
                return []
        
        # No query and no date → "All Patients" view (recent, bounded)
        return (
            query.order_by(order_fn(Patient.id))
            .limit(200)
            .all()
        )