# -*- coding: utf-8 -*-
# app/services/receipt_service.py
"""
Thermal (80mm) receipt generator for payments.
Produces a narrow PDF sized for X-printer thermal rolls.
Used on payment (print) and later (reprint) — same output either way.
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime

from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib import colors

from app.services.portal_reports.config import LAB_PROFILE
from app.models.payment import Payment
from app.models.patient import Patient
from app.models.test_request import TestRequest
from app.models.test_type import TestType
from app.services.payment_service import PaymentService


# 80mm thermal width; height grows with content
RECEIPT_WIDTH = 80 * mm


def _draw_center(c, x_center, y, text, font="Helvetica", size=8):
    c.setFont(font, size)
    c.drawCentredString(x_center, y, text)


def generate_receipt_pdf(db, payment: Payment) -> Path:
    """Builds an 80mm thermal receipt PDF for a payment and returns its path."""

    # Gather data
    patient = db.query(Patient).filter(Patient.id == payment.patient_id).first()
    request_ids = PaymentService.parse_request_ids(payment)

    line_items = []
    lab_number = None
    if request_ids:
        reqs = db.query(TestRequest).filter(TestRequest.id.in_(request_ids)).all()
        type_ids = list({r.test_type_id for r in reqs})
        types = db.query(TestType).filter(TestType.id.in_(type_ids)).all()
        tmap = {t.id: t for t in types}
        for r in reqs:
            tt = tmap.get(r.test_type_id)
            if r.lab_number and not lab_number:
                lab_number = r.lab_number
            line_items.append({
                "name": tt.name if tt else f"Test #{r.test_type_id}",
                "price": float(tt.price) if tt else 0.0,
            })

    cashier_name = ""
    if payment.created_by:
        cashier_name = getattr(payment.created_by, "username", "") or ""

    # ── Exact height calculation ──
    # The old code used a fixed "base = 120mm" guess, which was almost always
    # bigger than the content actually drawn. Since every element is drawn
    # starting from the TOP of the page and stepping downward, that excess
    # became blank space stranded at the BOTTOM of the page — which is what
    # printed as a long blank gap before/around the actual receipt content.
    # Instead, sum up exactly the vertical space every block below will use,
    # using the same conditions that gate each draw call.
    addr = LAB_PROFILE.get("address", "")
    addr_lines = _wrap(addr, 44)
    phone = LAB_PROFILE.get("phone", "").replace("|", "").strip()
    logo = LAB_PROFILE.get("logo_path")

    kv_count = 2  # Receipt No, Date (always drawn)
    if patient:
        kv_count += 2  # Patient, Patient No
    if lab_number:
        kv_count += 1
    if cashier_name:
        kv_count += 1

    discount_amount_est = max(
        sum(item["price"] for item in line_items) - float(payment.amount or 0), 0
    )

    top_margin = 8 * mm
    bottom_margin = 6 * mm

    content_h = 0.0
    if logo:
        content_h += 16 * mm + 3 * mm          # logo image + gap
    content_h += 4 * mm                        # lab name
    content_h += 3 * mm * len(addr_lines)      # address lines
    if phone:
        content_h += 3 * mm                    # phone
    content_h += 2 * mm + 5 * mm               # gaps around header separator
    content_h += 6 * mm                        # title
    content_h += 4 * mm * kv_count             # meta rows
    content_h += 1 * mm + 5 * mm               # gaps around meta separator
    content_h += 4 * mm                        # item header row
    content_h += 4 * mm * max(len(line_items), 1) if line_items else 0
    content_h += 1 * mm + 5 * mm               # gaps around items separator
    if discount_amount_est > 0:
        content_h += 4.5 * mm * 2              # subtotal + discount
    content_h += 4.5 * mm                      # total paid
    content_h += 6 * mm                        # method line
    content_h += 5 * mm                        # gap before footer separator
    content_h += 4 * mm                        # "thank you" line
    content_h += 4 * mm                        # "powered by" line

    height = top_margin + content_h + bottom_margin

    out_dir = Path("generated_reports")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"receipt_{payment.id}.pdf"

    c = canvas.Canvas(str(out_path), pagesize=(RECEIPT_WIDTH, height))
    cx = RECEIPT_WIDTH / 2
    y = height - 8 * mm

    # ── Logo ──
    if logo:
        try:
            from reportlab.lib.utils import ImageReader
            from PIL import Image
            img = Image.open(logo).convert("RGBA")
            reader = ImageReader(img)
            size = 16 * mm
            c.drawImage(reader, cx - size/2, y - size, size, size, mask="auto")
            y -= size + 3 * mm
        except Exception:
            pass

    # ── Header ──
    _draw_center(c, cx, y, LAB_PROFILE.get("lab_name", "Laboratory")[:38], "Helvetica-Bold", 7.5)
    y -= 4 * mm
    for chunk in addr_lines:
        _draw_center(c, cx, y, chunk, "Helvetica", 6)
        y -= 3 * mm
    if phone:
        _draw_center(c, cx, y, phone, "Helvetica", 6)
        y -= 3 * mm

    y -= 2 * mm
    c.setStrokeColor(colors.grey)
    c.setLineWidth(0.4)
    c.setDash(1, 2)
    c.line(6*mm, y, RECEIPT_WIDTH - 6*mm, y)
    c.setDash()
    y -= 5 * mm

    # ── Title ──
    _draw_center(c, cx, y, "PAYMENT RECEIPT", "Helvetica-Bold", 8)
    y -= 6 * mm

    # ── Meta ──
    c.setFont("Helvetica", 6.5)
    left = 6 * mm
    def kv(label, value):
        nonlocal y
        c.setFont("Helvetica", 6.5)
        c.drawString(left, y, label)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawRightString(RECEIPT_WIDTH - 6*mm, y, str(value))
        y -= 4 * mm

    kv("Receipt No:", f"RCP-{payment.id:05d}")
    kv("Date:", payment.created_at.strftime("%d %b %Y  %I:%M %p") if payment.created_at else "-")
    if patient:
        kv("Patient:", patient.full_name[:22])
        kv("Patient No:", patient.patient_no)
    if lab_number:
        kv("Lab No:", lab_number)
    if cashier_name:
        kv("Cashier:", cashier_name)

    y -= 1 * mm
    c.setDash(1, 2)
    c.line(6*mm, y, RECEIPT_WIDTH - 6*mm, y)
    c.setDash()
    y -= 5 * mm

    # ── Line items ──
    c.setFont("Helvetica-Bold", 6.5)
    c.drawString(left, y, "TEST")
    c.drawRightString(RECEIPT_WIDTH - 6*mm, y, "AMOUNT")
    y -= 4 * mm

    subtotal = 0.0
    for item in line_items:
        subtotal += item["price"]
        c.setFont("Helvetica", 6.5)
        name = item["name"][:30]
        c.drawString(left, y, name)
        c.drawRightString(RECEIPT_WIDTH - 6*mm, y, f"N{item['price']:,.0f}")
        y -= 4 * mm

    y -= 1 * mm
    c.setDash(1, 2)
    c.line(6*mm, y, RECEIPT_WIDTH - 6*mm, y)
    c.setDash()
    y -= 5 * mm

    # ── Totals ──
    amount_paid = float(payment.amount or 0)
    discount = max(subtotal - amount_paid, 0)

    def total_line(label, value, bold=False):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 7 if bold else 6.5)
        c.drawString(left, y, label)
        c.drawRightString(RECEIPT_WIDTH - 6*mm, y, f"N{value:,.0f}")
        y -= 4.5 * mm

    if discount > 0:
        total_line("Subtotal:", subtotal)
        total_line("Discount:", discount)
    total_line("TOTAL PAID:", amount_paid, bold=True)
    total_line("Method:", 0) if False else None
    c.setFont("Helvetica", 6.5)
    c.drawString(left, y, "Method:")
    c.drawRightString(RECEIPT_WIDTH - 6*mm, y, payment.method or "-")
    y -= 6 * mm

    # ── Footer ──
    c.setDash(1, 2)
    c.line(6*mm, y, RECEIPT_WIDTH - 6*mm, y)
    c.setDash()
    y -= 5 * mm
    _draw_center(c, cx, y, "Thank you for choosing us", "Helvetica-Oblique", 6.5)
    y -= 4 * mm
    _draw_center(c, cx, y, "Powered by Solunex Technologies", "Helvetica", 5.5)

    c.save()
    return out_path


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= width:
            cur = f"{cur} {w}".strip()
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines