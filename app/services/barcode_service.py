# -*- coding: utf-8 -*-
# app/services/barcode_service.py
"""
Barcode and QR code generation for LabCore v2.0 result PDFs.
Returns PIL Image objects ready for ReportLab's ImageReader.
"""
from __future__ import annotations

import io
from typing import Optional

from PIL import Image


def generate_qr(
    data: str,
    box_size: int = 4,
    border: int = 2,
) -> Image.Image:
    """
    Generates a QR code as a PIL Image.
    data: the URL or text to encode (e.g. portal result URL).
    """
    try:
        import qrcode
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=box_size,
            border=border,
        )
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        return img.convert("RGB")
    except ImportError:
        return _placeholder_image(100, 100, "QR")


def generate_barcode(
    code: str,
    bar_height: int = 30,
    bar_width: float = 1.2,
) -> Image.Image:
    """
    Generates a Code128 barcode as a PIL Image.
    code: the value to encode (result sync_id or report number).
    """
    try:
        import barcode
        from barcode.writer import ImageWriter

        # Sanitize — Code128 handles alphanumeric fine
        clean_code = str(code).replace("-", "")[:20]

        writer = ImageWriter()
        code128 = barcode.get("code128", clean_code, writer=writer)

        options = {
            "module_height": bar_height,
            "module_width": bar_width,
            "font_size": 6,
            "text_distance": 3,
            "quiet_zone": 2,
            "write_text": True,
        }

        buf = io.BytesIO()
        code128.write(buf, options=options)
        buf.seek(0)
        return Image.open(buf).convert("RGB")

    except ImportError:
        return _placeholder_image(200, 60, "BARCODE")
    except Exception as e:
        print(f"[Barcode] Generation error: {e}")
        return _placeholder_image(200, 60, str(code)[:12])


def _placeholder_image(w: int, h: int, label: str) -> Image.Image:
    """Returns a white placeholder image when barcode libs unavailable."""
    img = Image.new("RGB", (w, h), color="white")
    return img


def get_portal_qr_url(phone: str, patient_no: str, base_url: str = "https://iandelaboratory.com") -> str:
    """
    Builds the portal URL the QR code encodes.

    Previously pointed to /results/{sync_id} — broken two ways: that route
    expects an integer result id, not a sync_id (UUID), AND it requires an
    active login cookie regardless, so an anonymous scan would just bounce
    to /lookup anyway. Now points there directly, pre-filled with the
    patient's phone + patient-number suffix so scanning is a one-step
    login instead of a dead link.

    The suffix is derived the same way app/web/routes/portal_ui.py's
    login_action() reconstructs it (f"IEL-{year}-{suffix}") — whatever
    comes after the second hyphen in patient_no, e.g. "IEL-26-0001" -> "0001".
    """
    from urllib.parse import quote

    # patient_row uses "-" as a placeholder for a missing phone — don't embed that.
    clean_phone = (phone or "").strip()
    if clean_phone == "-":
        clean_phone = ""

    parts = (patient_no or "").split("-", 2)
    suffix = parts[-1] if len(parts) >= 3 else (patient_no or "")

    if not clean_phone or not suffix:
        return f"{base_url.rstrip('/')}/lookup"

    return (
        f"{base_url.rstrip('/')}/lookup"
        f"?phone={quote(clean_phone)}"
        f"&patient_suffix={quote(suffix)}"
    )