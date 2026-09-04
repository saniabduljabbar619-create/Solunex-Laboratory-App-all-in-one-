# -*- coding: utf-8 -*-
# app/services/portal_reports/renderer.py — LabCore v2.0
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import Table, TableStyle
from reportlab.lib.utils import ImageReader


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def _safe_set_alpha(c, a):
    try:
        c.setFillAlpha(a)
    except Exception:
        pass


def _parse_dt(raw) -> str:
    if not raw or raw == "N/A":
        return "N/A"
    try:
        if isinstance(raw, str):
            dt_obj = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt_obj.tzinfo is None:
                dt_obj = dt_obj.replace(tzinfo=timezone.utc)
        else:
            dt_obj = raw
            if dt_obj.tzinfo is None:
                dt_obj = dt_obj.replace(tzinfo=timezone.utc)
        return dt_obj.astimezone(None).strftime("%d %b %Y  %I:%M %p")
    except Exception:
        return str(raw)


def _pil_to_reader(pil_img) -> ImageReader:
    import io
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    buf.seek(0)
    return ImageReader(buf)


# --------------------------------------------------
# FLAG COLORS — v2.0 highlight system
# --------------------------------------------------
FLAG_COLORS = {
    "H": colors.HexColor("#CC0000"),   # red — high
    "L": colors.HexColor("#E87722"),   # amber — low
    "N": colors.black,                 # black — normal
}


def _flag_color(state: str):
    return FLAG_COLORS.get(str(state).upper(), colors.black)


# --------------------------------------------------
# HEADER
# --------------------------------------------------

def _draw_header(c, lab_profile, w, h):
    logo = lab_profile.get("logo_path")
    top_y = h - 20 * mm

    if logo:
        try:
            from PIL import Image
            pil_img = Image.open(logo)
            if pil_img.mode not in ("RGB", "RGBA"):
                pil_img = pil_img.convert("RGB")
            img = ImageReader(pil_img)
            size = 18 * mm
            c.drawImage(img, 15 * mm, h - 35 * mm, size, size, mask="auto")
            c.drawImage(img, w - 15 * mm - size, h - 35 * mm, size, size, mask="auto")
        except Exception as e:
            print(f"[PDF] Logo error: {e}")

    lab_name = lab_profile.get("lab_name", "Laboratory")
    address = lab_profile.get("address", "")
    phone = lab_profile.get("phone", "")
    email = lab_profile.get("email", "")

    c.setFont("Helvetica-Bold", 15)
    c.setFillColor(colors.HexColor("#1A6B3C"))
    c.drawCentredString(w / 2, top_y, lab_name)
    c.setFillColor(colors.black)

    c.setFont("Helvetica", 9)
    y_txt = top_y - 5 * mm
    if address:
        c.drawCentredString(w / 2, y_txt, address)
        y_txt -= 4 * mm
    contact = "  ".join([x for x in [phone, email] if x])
    if contact:
        c.drawCentredString(w / 2, y_txt, contact)

    c.setStrokeColor(colors.grey)
    c.setLineWidth(1)
    c.line(15 * mm, h - 42 * mm, w - 15 * mm, h - 42 * mm)


def _draw_continuation_header(c, w, page_height):
    """
    Continuation pages (page 2+) do NOT repeat the full letterhead — logo,
    lab name, address, contact — that's only meaningful once, at the top
    of page 1. Just leave a slim top margin with a divider so multi-page
    reports don't waste space (or look like a new document) on every page.
    """
    top_y = page_height - 15 * mm
    c.setStrokeColor(colors.lightgrey)
    c.setLineWidth(0.5)
    c.line(15 * mm, top_y, w - 15 * mm, top_y)


def _ensure_space(c, y, needed, page_height, lab_profile, w):
    if y - needed < 35 * mm:
        c.showPage()
        _draw_continuation_header(c, w, page_height)
        return page_height - 20 * mm
    return y


# --------------------------------------------------
# MAIN RENDER FUNCTION
# --------------------------------------------------

def render_pdf(
    output_path,
    lab_profile,
    patient_row,
    bundle_results,
    source="lab",
    requested_at=None,
    # v2.0 additions
    result_sync_id: str = None,
    report_number: str = None,
    scientist_name: str = None,
    scientist_qualification: str = None,
    sas_assisted: bool = False,
    portal_base_url: str = "https://iandelaboratory.com",
):
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(out), pagesize=A4)
    w, h = A4

    # ──────────── WATERMARK ────────────
    logo = lab_profile.get("logo_path")
    if lab_profile.get("watermark_enabled", True) and logo:
        try:
            from PIL import Image
            pil_img = Image.open(logo).convert("RGB")
            img = ImageReader(pil_img)
            _safe_set_alpha(c, 0.08)
            size = 140 * mm
            c.drawImage(img, (w - size) / 2, (h - size) / 2, size, size, mask="auto")
            _safe_set_alpha(c, 1)
        except Exception:
            pass

    # ──────────── HEADER ────────────
    _draw_header(c, lab_profile, w, h)


    # ──────────── PATIENT BLOCK ────────────
    pid = patient_row.get("Patient ID", "-")
    name = patient_row.get("Name", "-")
    sex = patient_row.get("Sex", "-")
    age = patient_row.get("Age", "-")

    lab_number = patient_row.get("Lab Number", "-")
    requested = patient_row.get("Requested", "-")
    reported = patient_row.get("Reported", "-")
    released = patient_row.get("Released", "-")

    c.setFont("Helvetica-Bold", 14)
    c.drawString(15 * mm, h - 48 * mm, "Patient Report")

    # SAS badge
    if sas_assisted:
        c.setFont("Helvetica-Bold", 7)
        c.setFillColor(colors.HexColor("#1A6B3C"))
        c.drawString(15 * mm, h - 54 * mm, "⬡ SAS ASSISTED")
        c.setFillColor(colors.black)

    # Row 1 — identity (Name/Sex/Lab Number, then Patient ID/Age) — bumped
    # from 9pt to 13pt so it's clearly legible on a printed page; the extra
    # vertical gaps below (10mm/7mm vs the old 9mm/4mm) are what keep the
    # bigger glyphs from touching each other or the title above.
    c.setFont("Helvetica", 13)
    c.drawString(15 * mm, h - 58 * mm, f"Name:       {name}")
    c.drawString(15 * mm, h - 65 * mm, f"Patient ID: {pid}")
    c.drawString(80 * mm, h - 58 * mm, f"Sex: {sex}")
    c.drawString(80 * mm, h - 65 * mm, f"Age: {age}")
    c.drawString(w - 75 * mm, h - 58 * mm, f"Lab Number:  {lab_number}")

    # Row 2 — accountability timeline (the metadata chain)
    c.setFont("Helvetica", 7.5)
    c.setFillColor(colors.HexColor("#555555"))
    c.drawString(15 * mm, h - 71 * mm, f"Requested: {requested}")
    c.drawString(80 * mm, h - 71 * mm, f"Reported: {reported}")
    c.drawString(w - 75 * mm, h - 71 * mm, f"Released: {released}")
    c.setFillColor(colors.black)

    # Divider below patient block (below the timeline row)
    c.setStrokeColor(colors.lightgrey)
    c.setLineWidth(0.5)
    c.line(15 * mm, h - 75 * mm, w - 15 * mm, h - 75 * mm)

    # Table starts below the divider
    y = h - 82 * mm

    # ──────────── RESULTS ────────────
    for rid, payload in bundle_results.items():
        test_name = payload.get("request", {}).get("test_name", "Test")
        typ = payload.get("type")
        heading_h = 6 * mm
        heading_drawn = False

        def _draw_heading():
            # Local helper so both branches below draw the heading the
            # same way, right after the space check that accounts for it.
            nonlocal y, heading_drawn
            c.setFont("Helvetica-Bold", 10)
            c.setFillColor(colors.black)
            c.drawString(15 * mm, y, test_name)
            y -= heading_h
            heading_drawn = True

        # ── STRUCTURED RESULT ──
        if typ == "structured":
            rows = payload.get("rows", [])
            header = ["Parameter", "Result", "Unit", "Ref Range", "Flag"]
            data = [header]
            row_flags = []

            for r in rows:
                flag_state = str(r.get("flag", "")).upper()
                row_flags.append(flag_state)
                data.append([
                    r.get("parameter", ""),
                    str(r.get("result", "")),
                    r.get("unit", ""),
                    r.get("ref_range", ""),
                    flag_state,
                ])

            tbl = Table(data, colWidths=[62 * mm, 25 * mm, 22 * mm, 33 * mm, 18 * mm])

            style = [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2C5F8A")),
                ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
                ("FONT",       (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE",   (0, 0), (-1, -1), 8),
                ("GRID",       (0, 0), (-1, -1), 0.3, colors.grey),
                ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN",      (1, 1), (-1, -1), "LEFT"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7F9FC")]),
            ]

            for i, flag_state in enumerate(row_flags):
                row_idx = i + 1
                if flag_state == "H":
                    style.append(("TEXTCOLOR", (1, row_idx), (1, row_idx), colors.HexColor("#CC0000")))
                    style.append(("FONT", (1, row_idx), (1, row_idx), "Helvetica-Bold"))
                    style.append(("TEXTCOLOR", (4, row_idx), (4, row_idx), colors.HexColor("#CC0000")))
                elif flag_state == "L":
                    style.append(("TEXTCOLOR", (1, row_idx), (1, row_idx), colors.HexColor("#E87722")))
                    style.append(("FONT", (1, row_idx), (1, row_idx), "Helvetica-Bold"))
                    style.append(("TEXTCOLOR", (4, row_idx), (4, row_idx), colors.HexColor("#E87722")))

            tbl.setStyle(TableStyle(style))
            # Measure the table BEFORE drawing anything — availHeight is
            # just the full page height here since we're only using this
            # to get the table's natural (unsplit) height, not to fit it.
            tw, th = tbl.wrapOn(c, w - 30 * mm, h)

            # Reserve heading + table TOGETHER. If they don't both fit,
            # page-break now, before the heading is drawn, so the two
            # can never end up split across pages.
            y = _ensure_space(c, y, heading_h + th + 10 * mm, h, lab_profile, w)
            _draw_heading()
            tbl.drawOn(c, 15 * mm, y - th)
            y -= th + 8 * mm

        # ── TABLE / GRID RESULT ──
        elif typ == "table":
            sections = payload.get("uix", {}).get("sections") or [payload.get("grid", {})]

            for section_grid in sections:
                cells = section_grid.get("cells", [])
                if not cells:
                    continue

                title = section_grid.get("title", "")
                title_h = 5 * mm if title else 0

                ncols = max(len(r) for r in cells)
                padded = [r + [""] * (ncols - len(r)) for r in cells]
                col_width = (w - 30 * mm) / ncols

                tbl = Table(padded, colWidths=[col_width] * ncols)
                tbl.setStyle(TableStyle([
                    ("GRID",       (0, 0), (-1, -1), 0.3, colors.grey),
                    ("FONT",       (0, 0), (-1,  0), "Helvetica-Bold"),
                    ("BACKGROUND", (0, 0), (-1,  0), colors.HexColor("#2C5F8A")),
                    ("TEXTCOLOR",  (0, 0), (-1,  0), colors.white),
                    ("FONTSIZE",   (0, 0), (-1, -1), 8),
                    ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
                    ("ALIGN",      (0, 0), ( 0, -1), "LEFT"),
                    ("ALIGN",      (1, 0), (-1, -1), "LEFT"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                     [colors.white, colors.HexColor("#F7F9FC")]),
                ]))

                # Measure this section's table before committing to
                # anything above it, same principle as the structured
                # branch: page-break ahead of the heading/title, never
                # in between it and its own table.
                tw, th = tbl.wrapOn(c, w - 30 * mm, h)

                needed = title_h + th + 10 * mm
                if not heading_drawn:
                    # First section in this test also carries the outer
                    # test-name heading — bundle its height into the
                    # same space check so heading + title + table all
                    # move to the next page together, never separately.
                    needed += heading_h

                y = _ensure_space(c, y, needed, h, lab_profile, w)

                if not heading_drawn:
                    _draw_heading()

                if title:
                    c.setFont("Helvetica-BoldOblique", 8)
                    c.setFillColor(colors.HexColor("#2C5F8A"))
                    c.drawString(15 * mm, y, f"[{title}]")
                    c.setFillColor(colors.black)
                    y -= title_h

                tbl.drawOn(c, 15 * mm, y - th)
                y -= th + 10 * mm

            if not heading_drawn:
                # No section had any cells — still show the heading so a
                # test entry never silently disappears from the report.
                y = _ensure_space(c, y, heading_h + 4 * mm, h, lab_profile, w)
                _draw_heading()

        else:
            # Unknown/unsupported payload type — still print the heading
            # rather than dropping the test entry entirely.
            y = _ensure_space(c, y, heading_h + 4 * mm, h, lab_profile, w)
            _draw_heading()

        # Separator between tests
        c.setStrokeColor(colors.lightgrey)
        c.line(15 * mm, y, w - 15 * mm, y)
        y -= 6 * mm

    # ──────────── SIGNATURE + STAMP + QR — one shared row, anchored to a fixed
    # position near the bottom (matches where the QR code already sat), so all
    # three always line up together instead of the stamp/signature floating
    # wherever the results table happened to end while the QR stayed fixed.
    # Order: Signature (left) | Stamp (centered in the middle gap) | QR (right).
    sci_name = scientist_name or lab_profile.get("scientist_name", "")
    sci_qual = scientist_qualification or lab_profile.get("scientist_qualification", "")

    row_bottom = 20 * mm
    row_top = 42 * mm       # 22mm tall — same height as the QR code and the stamp box
    qr_size = 22 * mm
    stamp_w, stamp_h = 42 * mm, 22 * mm
    sig_width = 60 * mm

    # All X positions computed upfront, before any drawing — so the stamp's
    # centering is correct even if QR generation fails below.
    sig_left = 15 * mm
    sig_right_edge = sig_left + sig_width
    qr_x = w - 15 * mm - qr_size  # right-aligned to the margin
    stamp_x = sig_right_edge + ((qr_x - sig_right_edge - stamp_w) / 2)

    # LEFT — scientist signature (left-aligned now that it's the leftmost block)
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.5)
    c.line(sig_left, row_top - 2 * mm, sig_left + sig_width, row_top - 2 * mm)
    c.setFont("Helvetica-Bold", 8)
    if sci_name:
        c.drawString(sig_left, row_top - 6 * mm, sci_name)
    if sci_qual:
        c.setFont("Helvetica", 7)
        c.drawString(sig_left, row_top - 10 * mm, sci_qual)
    c.setFont("Helvetica", 6.5)
    c.setFillColor(colors.grey)
    c.drawString(sig_left, row_bottom + 3 * mm, "Verified & Authorized by (Medical Laboratory Scientist)")
    c.setFillColor(colors.black)

    # MIDDLE — official stamp box, centered between signature and QR
    c.setStrokeColor(colors.grey)
    c.setLineWidth(0.6)
    c.rect(stamp_x, row_bottom, stamp_w, stamp_h)
    c.setFont("Helvetica-Oblique", 6.5)
    c.setFillColor(colors.grey)
    c.drawCentredString(stamp_x + stamp_w / 2, row_bottom + stamp_h / 2, "Official Lab Stamp")
    c.setFillColor(colors.black)

    # Note: the plaintext "Portal Login / User / Pass" block that used to
    # sit here was removed — it was redundant (and a needless exposure of
    # the patient's login credential in plain text on paper) since the QR
    # code below is already pre-filled with phone + patient number for a
    # one-step login. Scanning it gets you there without reading anything.


    # ──────────── QR CODE — RIGHT, same row as stamp + signature above ────────────
    # Points at the login page, pre-filled with phone + patient-number suffix
    # so scanning is a one-step login, not just a bare link the patient still
    # has to type their details into. A per-result deep link would just
    # redirect to /lookup anyway (no session cookie when scanning a printed
    # page), so pre-filling the login itself is the actual useful behavior.
    try:
        from app.services.barcode_service import generate_qr, get_portal_qr_url
        qr_url = get_portal_qr_url(
            phone=patient_row.get("Phone", ""),
            patient_no=patient_row.get("Patient ID", ""),
            base_url=portal_base_url,
        )
        qr_img = generate_qr(qr_url, box_size=3, border=1)
        qr_reader = _pil_to_reader(qr_img)
        c.drawImage(
            qr_reader,
            qr_x,
            row_bottom,
            qr_size, qr_size,
        )
        c.setFont("Helvetica", 6)
        c.setFillColor(colors.grey)
        c.drawString(qr_x, row_bottom - 2 * mm, "Scan to verify result online")
        c.setFillColor(colors.black)
    except Exception as e:
        print(f"[PDF] QR error: {e}")

    # ──────────── FOOTER ────────────
    # ──────────── FOOTER (clinical notes only) ────────────
    c.setStrokeColor(colors.grey)
    c.setLineWidth(0.5)
    c.line(15 * mm, 16 * mm, w - 15 * mm, 16 * mm)

    c.setFont("Helvetica-Oblique", 7)
    c.setFillColor(colors.grey)
    clinical_note = lab_profile.get(
        "clinical_note",
        "Results relate only to the specimen received. Please correlate clinically. "
        "Consult your physician for interpretation.",
    )
    c.drawCentredString(w / 2, 11 * mm, clinical_note)
    c.setFillColor(colors.black)
    if source == "lab":
        source_note = "Official Reprint: Routed and fetched from the Laboratory Internal Portal."
    else:
        source_note = "Online Document: Electronically generated and downloaded via the Patient Portal."
    c.drawCentredString(w / 2, 7 * mm, source_note)

    c.save()
    return str(out)