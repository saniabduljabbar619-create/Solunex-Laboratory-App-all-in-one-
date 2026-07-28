# -*- coding: utf-8 -*-
# app/web/routes/portal_ui.py
from __future__ import annotations

from fastapi import APIRouter, Request, Form, Response, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime, timedelta, timezone
from fastapi import Form
import json
from app.core.config import settings
from app.api.deps import get_db
from app.services.portal_service import PortalService
from app.services.test_type_service import TestTypeService
from app.services.booking_service import BookingService
from app.models.booking import Booking
from app.models.booking_item import BookingItem
from app.models.payment_proof_model import PaymentProof
from fastapi import UploadFile, File
from sqlalchemy.orm import Session
from fastapi import Depends
import os
from uuid import uuid4
from pathlib import Path
from fastapi.responses import FileResponse
from datetime import datetime
from app.services.portal_reports.config import LAB_PROFILE
from app.services.portal_reports.builder import build_bundle_result
from app.services.portal_reports.renderer import render_pdf

from pydantic import BaseModel
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")
UPLOAD_DIR = "uploads/payments"
lab_profile = LAB_PROFILE



# ===============================
# LOOKUP (Login Page)
# ===============================
@router.get("/lookup", response_class=HTMLResponse)
def login_page(request: Request, phone: str = None, patient_suffix: str = None):
    return templates.TemplateResponse(
        "portal/lookup.html",
        {"request": request, "phone": phone, "patient_suffix": patient_suffix}
    )


@router.post("/login")
def login_action(
    request: Request,
    phone: str = Form(...),
    # Changed from patient_no to patient_suffix to match the new HTML
    patient_suffix: str = Form(...) 
):
    db = next(get_db())
    service = PortalService(db, settings.PORTAL_SECRET)

    # --- RECONSTRUCTION LOGIC ---
    # Re-attach the IEL and current Year (e.g., 3150 -> IEL-26-3150)
    current_year_yy = datetime.now().strftime("%y")
    full_patient_no = f"IEL-{current_year_yy}-{patient_suffix.strip()}"
    # ----------------------------

    try:
        # Pass the RECONSTRUCTED number to your service
        auth_data = service.login(phone=phone, patient_no=full_patient_no)
    except Exception:
        return templates.TemplateResponse(
            "portal/lookup.html",
            {
                "request": request,
                "error": "Invalid phone number or patient number"
            }
        )

    token = auth_data["token"]

    response = RedirectResponse(url="/results", status_code=303)
    response.set_cookie(
        key="portal_token",
        value=token,
        httponly=True,
        max_age=900 # 15 minutes session
    )
    return response

# ===============================
# LOGOUT (PATIENT)
# ===============================
@router.get("/logout")
def patient_logout():
    response = RedirectResponse("/lookup", status_code=303)
    response.delete_cookie("portal_token")
    return response

# ===============================
# HOME PAGE
# ===============================
@router.get("/", response_class=HTMLResponse)
def home_page(request: Request):
    return templates.TemplateResponse(
        "portal/home.html",
        {"request": request}
    )


# ===============================
# RESULTS PAGE
# ===============================
@router.get("/results", response_class=HTMLResponse)
def results_page(request: Request, db: Session = Depends(get_db)):

    token = request.cookies.get("portal_token")
    if not token:
        return RedirectResponse("/lookup", status_code=303)

    service = PortalService(db, settings.PORTAL_SECRET)

    try:
        patient_id = service.verify_token(token)
    except Exception:
        return RedirectResponse("/lookup", status_code=303)

    patient = service.get_patient_profile(patient_id)
    results = service.list_released_results(patient_id)

    # Group results by SSDO test category for the category cards
    results_by_category: dict[str, list] = {}
    for r in results:
        cat = r.get("test_category") or "General"
        results_by_category.setdefault(cat, []).append(r)

    return templates.TemplateResponse(
        "portal/results.html",
        {
            "request": request,
            "patient": patient,
            "results": results,
            "results_by_category": results_by_category,
        }
    )

# ===============================
# RESULT VIEW PAGE
# ===============================

@router.get("/results/{result_id}", response_class=HTMLResponse)
def view_result(request: Request, result_id: int):

    token = request.cookies.get("portal_token")
    if not token:
        return RedirectResponse("/lookup", status_code=303)

    db = next(get_db())
    service = PortalService(db, settings.PORTAL_SECRET)

    patient_id = service.verify_token(token)

    result = service.get_released_result(patient_id, result_id)
    ssfo = service.get_ssdo_for_result(result_id)

    return templates.TemplateResponse(
        "portal/result_view.html",
        {
            "request": request,
            "result": result,
            "ssfo": ssfo,
        }
    )


# ===============================
# RESULT DOWNLOAD
# ===============================

@router.get("/results/{result_id}/download")
def download_result(request: Request, result_id: int):

    token = request.cookies.get("portal_token")

    if not token:
        return RedirectResponse("/lookup", status_code=303)

    db = next(get_db())
    service = PortalService(db, settings.PORTAL_SECRET)

    patient_id = service.verify_token(token)

    result = service.get_released_result(patient_id, result_id)

    # ===============================
    # BUILD BUNDLE RESULT
    # ===============================

    payload = build_bundle_result(result)

    bundle_results = {
        str(result.id): payload
    }

    # ===============================
    # PATIENT DATA
    # ===============================

    patient = result.patient

    # gender → Sex
    sex = patient.gender if patient.gender else "-"

    # calculate Age from date_of_birth
    age = "-"

    if patient.date_of_birth:
        today = datetime.today()
        age = (
            today.year
            - patient.date_of_birth.year
            - ((today.month, today.day) < (patient.date_of_birth.month, patient.date_of_birth.day))
        )

    patient_row = {
        "Patient ID": patient.patient_no,
        "Name": patient.full_name,
        "Sex": sex,
        "Age": age,
        "Phone": patient.phone if patient.phone else "-"
    }

    # ===============================
    # LAB PROFILE (PORTAL SIDE CONFIG)
    # ===============================

    from pathlib import Path
    BASE_DIR = Path(__file__).resolve().parents[3]

    logo_file = BASE_DIR /"app" / "web" / "static" / "logo.png"

    lab_profile = {
        "lab_name": "I and E Diagnostic Laboratory and Ultra Sound Scan",
        "address": "NO : 001 Na'ibawa, Kano - Zaria Road, Tarauni, Kano, Nigeria",
        "phone": "08063645308 | ",
        "email": "iandelaboratory@yahoo.com",
        "logo_path": str(logo_file),
        "watermark_enabled": True,
        "report_notes": "Online downloadable result for reference only. For official verification visit the laboratory.",
    }
    # ===============================
    # OUTPUT PATH
    # ===============================

    output_dir = Path("generated_reports")
    output_dir.mkdir(exist_ok=True)

    filename = f"result_{result.id}.pdf"

    output_path = output_dir / filename

    # ===============================
    # RENDER PDF
    # ===============================
    print("LOGO PATH:", logo_file)
    print("LOGO EXISTS:", logo_file.exists())

    from app.services.result_pdf_service import generate_result_pdf
    output_path = generate_result_pdf(result, requested_at=result.created_at)

    # ===============================
    # DOWNLOAD RESPONSE
    # ===============================

    return FileResponse(
        path=output_path,
        media_type="application/pdf",
        filename=filename,
    )


# ===============================
# BOOKING PAGE
# ===============================
@router.get("/book", response_class=HTMLResponse)
def booking_page(request: Request):
    db = next(get_db())
    service = TestTypeService(db)

    # If you have is_active column
    try:
        tests = service.list_active()
    except AttributeError:
        tests = service.list()

    # -----------------------------
    # GROUP TESTS BY CATEGORY
    # -----------------------------
    tests_by_category = {}

    for test in tests:

        # if model has category field
        category = getattr(test, "category", None) or "Laboratory Tests"

        if category not in tests_by_category:
            tests_by_category[category] = []

        tests_by_category[category].append(test)

    return templates.TemplateResponse(
        "portal/book.html",
        {
            "request": request,
            "tests_by_category": tests_by_category
        }
    )



@router.post("/book")
def book_action(
    request: Request,

    action: str = Form(...),   # ✅ NEW

    booking_type: str = Form("single"),

    full_name: str = Form(None),
    phone: str = Form(None),
    dob: str = Form(None),
    email: str = Form(None),
    gender: str = Form(None),

    tests: list[int] = Form([]),

    referrer_name: str = Form(None),
    referrer_phone: str = Form(None),

    group_payload: str = Form(None)
):

    db = next(get_db())
    service = BookingService(db)

    # ======================================================
    # GROUP BOOKING FLOW
    # ======================================================

    if booking_type == "group":

        items = []

        try:
            payload = json.loads(group_payload or "[]")
        except:
            payload = []

        for row in payload:
            items.append({
                "patient_name": row.get("patient_name"),
                "patient_phone": row.get("patient_phone"),
                "test_type_id": row.get("test_type_id"),
                "test_name": row.get("test_name"),
                "price": row.get("price")
            })

        booking = service.create_group_booking(
            referrer_name=referrer_name,
            email=email,
            referrer_phone=referrer_phone,
            items=items
        )

    # ======================================================
    # PERSONAL BOOKING FLOW
    # ======================================================

    else:

        patient_dob = datetime.strptime(dob, "%Y-%m-%d").date()

        items = []

        for test_id in tests:
            items.append({
                "patient_name": full_name,
                "patient_phone": phone,
                "dob": patient_dob,
                "gender": gender,
                "test_type_id": test_id
            })

        booking = service.create_booking(
            booking_type="single",
            referrer_name=None,
            referrer_phone=None,
            email=email,
            items=items
        )

    # ======================================================
    # ACTION HANDLING (NEW LOGIC)
    # ======================================================

    if action == "pay":
        return RedirectResponse(
            url=f"/payment/{booking.booking_code}",
            status_code=303
        )

    elif action == "unpaid":
        booking.status = "pending_approval"
        db.commit()

        return RedirectResponse(
            url="/book?submitted=1",
            status_code=303
        )

# ===============================
# PAYMENT PAGE
# ===============================
@router.get("/payment/{booking_code}", response_class=HTMLResponse)
def booking_payment_page(request: Request, booking_code: str):

    db = next(get_db())

    booking = (
        db.query(Booking)
        .filter(Booking.booking_code == booking_code)
        .first()
    )

    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    items = (
        db.query(BookingItem)
        .filter(BookingItem.booking_id == booking.id)
        .all()
    )

    return templates.TemplateResponse(
        "portal/payment.html",
        {
            "request": request,
            "booking": booking,
            "items": items,
        },
    )





@router.post("/payment/{booking_code}")
async def upload_payment_proof(
    request: Request,
    booking_code: str,
    proof: UploadFile = File(...)
):

    print("UPLOAD ROUTE HIT:", booking_code, proof.filename)

    db = next(get_db())

    booking = (
        db.query(Booking)
        .filter(Booking.booking_code == booking_code)
        .first()
    )

    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    # ensure upload directory exists
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    # generate unique filename
    ext = proof.filename.split(".")[-1]
    filename = f"{booking_code}_{uuid4().hex}.{ext}"

    file_path = os.path.join(UPLOAD_DIR, filename)

    # save file
    with open(file_path, "wb") as buffer:
        buffer.write(await proof.read())

    # --------------------------------------------------
    # INSERT PAYMENT PROOF RECORD
    # --------------------------------------------------

    payment_proof = PaymentProof(
        booking_id=booking.id,
        file_path=file_path,
        status="pending"
    )

    db.add(payment_proof)

    # --------------------------------------------------
    # UPDATE BOOKING STATUS
    # --------------------------------------------------

    booking.status = "awaiting_verification"

    db.commit()

    return RedirectResponse(
        url=f"/payment/{booking.booking_code}?uploaded=1",
        status_code=303
    )


# ===============================
# CONTACT PAGE
# ===============================

@router.get("/contact", response_class=HTMLResponse)
def contact_page(request: Request):
    return templates.TemplateResponse(
        "portal/contact.html",
        {"request": request}
    )


@router.post("/contact")
def contact_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    subject: str = Form(...),
    message: str = Form(...)
):

    # For now we just acknowledge submission
    # Later we can store or send email

    return RedirectResponse(
        url="/contact?sent=1",
        status_code=303
    )



# ===============================
# TESTS CATALOG PAGE
# ===============================

@router.get("/tests", response_class=HTMLResponse)
def tests_page(request: Request):
    db = next(get_db())
    service = TestTypeService(db)

    try:
        tests = service.list_active()
    except AttributeError:
        tests = service.list()

    tests_by_category = {}

    for test in tests:

        category = getattr(test, "category", None) or "Laboratory Tests"

        if category not in tests_by_category:
            tests_by_category[category] = []

        tests_by_category[category].append(test)

    return templates.TemplateResponse(
        "portal/tests.html",
        {
            "request": request,
            "tests_by_category": tests_by_category
        }
    )


# ===============================
# REFERRER LOGIN PAGE
# ===============================
# ===============================
# REFERRER LOGIN PAGE
# ===============================
@router.get("/referrer/login", response_class=HTMLResponse)
def referrer_login_page(request: Request):
    return templates.TemplateResponse("portal/referrer_login.html", {"request": request})


@router.post("/referrer/login")
def referrer_login_action(request: Request, phone: str = Form(...), code: str = Form(...)):
    db = next(get_db())
    from app.models.referrer import Referrer
    from app.core.security import verify_password

    phone_clean = phone.strip()
    referrer = db.query(Referrer).filter(Referrer.phone == phone_clean).first()

    valid = False
    if referrer and referrer.portal_code and referrer.portal_code_expires_at:
        not_expired = datetime.now(timezone.utc) < referrer.portal_code_expires_at.replace(tzinfo=timezone.utc)
        if not_expired and verify_password(code.strip(), referrer.portal_code):
            valid = True

    if not valid:
        return templates.TemplateResponse(
            "portal/referrer_login.html",
            {
                "request": request,
                "phone": phone_clean,
                "error": "Invalid or expired code. Re-enter your phone number for a new one.",
            }
        )

    referrer.portal_code = None
    referrer.portal_code_expires_at = None
    db.commit()

    portal_service = PortalService(db, settings.PORTAL_SECRET)
    token = portal_service._sign({
        "referrer_id": referrer.id,
        "scope": "referrer",
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    })

    response = RedirectResponse(url="/referrer/dashboard", status_code=303)
    response.set_cookie(key="referrer_token", value=token, httponly=True, max_age=3600)
    return response


@router.get("/referrer/logout")
def referrer_logout():
    response = RedirectResponse("/referrer/login", status_code=303)
    response.delete_cookie("referrer_token")
    return response


# ===============================
# VERIFY REFERRER TOKEN (HELPER)
# ===============================
def _verify_referrer(request: Request):
    token = request.cookies.get("referrer_token")

    if not token:
        return None

    db = next(get_db())
    portal_service = PortalService(db, settings.PORTAL_SECRET)

    try:
        payload = portal_service._verify(token)

        if payload.get("exp") < int(datetime.now(timezone.utc).timestamp()):
            return None

        if payload.get("scope") != "referrer":
            return None

        return payload.get("referrer_id")

    except Exception:
        return None


# ===============================
# VERIFY REFERRER TOKEN (HELPER)
# ===============================
def _verify_referrer(request: Request):
    token = request.cookies.get("referrer_token")

    if not token:
        return None

    db = next(get_db())
    portal_service = PortalService(db, settings.PORTAL_SECRET)

    try:
        payload = portal_service._verify(token)

        # EXPIRY CHECK (you must do this manually here)
        if payload.get("exp") < int(datetime.now(timezone.utc).timestamp()):
            return None

        if payload.get("scope") != "referrer":
            return None

        return payload.get("referrer_id")

    except Exception:
        return None


# ===============================
# REFERRER DASHBOARD
# ===============================
@router.get("/referrer/dashboard", response_class=HTMLResponse)
def referrer_dashboard(request: Request):
    referrer_id = _verify_referrer(request)

    if not referrer_id:
        return RedirectResponse("/referrer/login", status_code=303)

    db = next(get_db())

    from app.services.referrer_profile_service import ReferrerProfileService
    svc = ReferrerProfileService(db)

    profile = svc.get_profile(referrer_id)
    patients_data = svc.get_referred_patients(referrer_id, limit=100)
    insights = svc.get_ssdo_insights(referrer_id)

    return templates.TemplateResponse(
        "portal/referrer_dashboard.html",
        {
            "request": request,
            "profile": profile,
            "patients": patients_data["patients"],
            "total_patients": patients_data["total"],
            "insights": insights,
        }
    )


# ===============================
# REFERRER AVATAR UPLOAD (from dashboard)
# ===============================
@router.post("/referrer/avatar")
async def referrer_avatar_upload(request: Request, avatar: UploadFile = File(...)):
    referrer_id = _verify_referrer(request)
    if not referrer_id:
        return RedirectResponse("/referrer/login", status_code=303)

    db = next(get_db())
    from app.services.referrer_profile_service import ReferrerProfileService
    svc = ReferrerProfileService(db)
    svc.upload_avatar(referrer_id, avatar)

    return RedirectResponse("/referrer/dashboard", status_code=303)

# ===============================
# REFERRER — test catalog (for booking more tests)
# ===============================
@router.get("/referrer/tests")
def referrer_test_catalog(request: Request):
    referrer_id = _verify_referrer(request)
    if not referrer_id:
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)

    db = next(get_db())
    from app.services.test_type_service import TestTypeService
    service = TestTypeService(db)
    try:
        tests = service.list_active()
    except AttributeError:
        tests = service.list()

    return JSONResponse({
        "ok": True,
        "tests": [
            {
                "id": t.id,
                "name": t.name,
                "price": float(t.price or 0),
                "category": getattr(t, "category", None) or "General",
            }
            for t in tests
        ],
    })


# ===============================
# REFERRER RESULT DOWNLOAD (from dashboard)
# ===============================
@router.get("/referrer/patients/{patient_id}/results/{result_id}/download")
def referrer_result_download(request: Request, patient_id: int, result_id: int):
    referrer_id = _verify_referrer(request)
    if not referrer_id:
        return RedirectResponse("/referrer/login", status_code=303)

    db = next(get_db())
    from app.services.referrer_profile_service import ReferrerProfileService
    svc = ReferrerProfileService(db)
    pdf_path = svc.download_patient_result(referrer_id, patient_id, result_id)

    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=f"result_{result_id}.pdf",
    )


# ===============================
# REFERRER BOOKINGS
# ===============================
@router.get("/referrer/bookings", response_class=HTMLResponse)
def referrer_bookings(request: Request):
    referrer_id = _verify_referrer(request)

    if not referrer_id:
        return RedirectResponse("/referrer/login", status_code=303)

    db = next(get_db())

    from app.services.referrer_service import ReferrerService
    service = ReferrerService(db)

    bookings = service.list_bookings(referrer_id)

    return templates.TemplateResponse(
        "portal/referrer_bookings.html",
        {
            "request": request,
            "bookings": bookings
        }
    )


# ===============================
# REFERRER CREDIT VIEW
# ===============================
@router.get("/referrer/credits", response_class=HTMLResponse)
def referrer_credits(request: Request):
    referrer_id = _verify_referrer(request)

    if not referrer_id:
        return RedirectResponse("/referrer/login", status_code=303)

    db = next(get_db())

    from app.services.referrer_service import ReferrerService
    service = ReferrerService(db)

    credit = service.get_credit_status(referrer_id)

    return templates.TemplateResponse(
        "portal/referrer_credits.html",
        {
            "request": request,
            "credit": credit
        }
    )




# ===============================
# REFERRER — fetch a fresh on-screen access code for a phone number
# ===============================
@router.post("/referrer/get-code")
async def referrer_get_code(request: Request):
    from fastapi.responses import JSONResponse
    body = await request.json()
    phone = (body.get("phone") or "").strip()

    db = next(get_db())
    from app.models.referrer import Referrer
    from app.core.security import hash_password
    import secrets

    referrer = db.query(Referrer).filter(Referrer.phone == phone).first()
    if not referrer:
        # Same shape whether or not the number is registered
        return JSONResponse({"ok": False})

    code = f"{secrets.randbelow(1000000):06d}"
    referrer.portal_code = hash_password(code)
    referrer.portal_code_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    db.commit()

    return JSONResponse({"ok": True, "code": code})


class ReferrerBookTestsIn(BaseModel):
    test_type_ids: list[int]


@router.post("/referrer/patients/{patient_id}/book-tests")
def referrer_book_tests(request: Request, patient_id: int, payload: ReferrerBookTestsIn):
    referrer_id = _verify_referrer(request)
    if not referrer_id:
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)

    if not payload.test_type_ids:
        return JSONResponse({"ok": False, "error": "No tests selected"}, status_code=400)

    db = next(get_db())
    from app.services.referrer_profile_service import ReferrerProfileService
    from app.models.patient import Patient
    from app.models.test_request import TestRequest
    from app.services.numbering_service import NumberingService

    svc = ReferrerProfileService(db)

    # Ownership check — same pattern as result download: this patient must
    # actually be referred by this referrer.
    data = svc.get_referred_patients(referrer_id, limit=9999)
    referred_ids = {p["patient_id"] for p in data["patients"]}
    if patient_id not in referred_ids:
        return JSONResponse(
            {"ok": False, "error": "This patient was not referred by you."},
            status_code=403,
        )

    patient = db.query(Patient).filter(Patient.id == patient_id).first()
    if not patient:
        return JSONResponse({"ok": False, "error": "Patient not found"}, status_code=404)

    referrer = svc.get_referrer(referrer_id)
    lab_number = NumberingService(db).next_lab_number()

    created = []
    for tt_id in payload.test_type_ids:
        req = TestRequest(
            patient_id=patient.id,
            test_type_id=tt_id,
            requested_by=f"Referrer: {referrer.name}",
            status="pending",
            lab_number=lab_number,
            branch_id=patient.branch_id,
        )
        db.add(req)
        created.append(req)
    db.commit()

    return JSONResponse({
        "ok": True,
        "lab_number": lab_number,
        "count": len(created),
    })