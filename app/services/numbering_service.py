# -*- coding: utf-8 -*-
# app/services/numbering_service.py
"""
Configurable numbering for Patient No and Lab No.
Formats are templates with tokens; sequences reset per policy.
Config stored in SystemConfig. Used at patient creation (patient no)
and test request (lab no).

Tokens: {PREFIX} {YY} {YYYY} {MM} {DD} {SEQ:n}
Reset policies: never | yearly | monthly | daily
"""
from __future__ import annotations

import re
from datetime import date
from sqlalchemy.orm import Session

from app.models.system_config import SystemConfig


DEFAULTS = {
    "patient_number_format": "IEL-{YY}-{SEQ:4}",
    "patient_reset_policy": "yearly",
    "lab_number_format": "LAB-{SEQ:4}",
    "lab_reset_policy": "yearly",
}


class NumberingService:
    def __init__(self, db: Session):
        self.db = db

    # ── config get/set ──
    def get(self, key: str, default: str = "") -> str:
        row = self.db.query(SystemConfig).filter(SystemConfig.key == key).first()
        if row and row.value is not None:
            return row.value
        return DEFAULTS.get(key, default)

    def set(self, key: str, value: str) -> None:
        row = self.db.query(SystemConfig).filter(SystemConfig.key == key).first()
        if row:
            row.value = value
        else:
            row = SystemConfig(key=key, value=value)
            self.db.add(row)
        self.db.commit()

    def get_all_config(self) -> dict:
        return {k: self.get(k) for k in DEFAULTS.keys()}

    def save_config(self, data: dict) -> dict:
        for k in DEFAULTS.keys():
            if k in data and data[k] is not None:
                self.set(k, str(data[k]))
        return self.get_all_config()

    # ── period key for reset ──
    def _period_key(self, policy: str, today: date) -> str:
        if policy == "daily":
            return today.strftime("%Y%m%d")
        if policy == "monthly":
            return today.strftime("%Y%m")
        if policy == "yearly":
            return today.strftime("%Y")
        return "all"   # never resets

    # ── sequence counter (stored per format+period) ──
    def _next_seq(self, counter_key: str) -> int:
        full_key = f"seq::{counter_key}"
        row = self.db.query(SystemConfig).filter(SystemConfig.key == full_key).first()
        if row and row.value and row.value.isdigit():
            nxt = int(row.value) + 1
            row.value = str(nxt)
        else:
            nxt = 1
            if row:
                row.value = "1"
            else:
                row = SystemConfig(key=full_key, value="1")
                self.db.add(row)
        self.db.commit()
        return nxt

    # ── render a format template ──
    def render(self, fmt: str, seq: int, today: date | None = None) -> str:
        today = today or date.today()
        out = fmt
        out = out.replace("{PREFIX}", "")  # PREFIX is baked into the literal text
        out = out.replace("{YYYY}", today.strftime("%Y"))
        out = out.replace("{YY}", today.strftime("%y"))
        out = out.replace("{MM}", today.strftime("%m"))
        out = out.replace("{DD}", today.strftime("%d"))
        # {SEQ:n} → zero-padded
        def seq_sub(m):
            width = int(m.group(1)) if m.group(1) else 1
            return str(seq).zfill(width)
        out = re.sub(r"\{SEQ:?(\d*)\}", seq_sub, out)
        return out

    def preview(self, fmt: str) -> str:
        """Render with a sample sequence of 1 for the settings live preview."""
        return self.render(fmt, 1)

    def validate(self, fmt: str) -> tuple[bool, str]:
        """A format MUST contain a {SEQ} token to avoid duplicates."""
        if not re.search(r"\{SEQ:?\d*\}", fmt):
            return False, "Format must include a {SEQ} token (e.g. {SEQ:4})."
        return True, "OK"

    # ── the two public generators ──
    def next_patient_number(self) -> str:
        fmt = self.get("patient_number_format")
        policy = self.get("patient_reset_policy")
        today = date.today()
        pkey = self._period_key(policy, today)
        counter_key = f"patient::{fmt}::{pkey}"
        seq = self._next_seq(counter_key)
        return self.render(fmt, seq, today)

    def next_lab_number(self) -> str:
        fmt = self.get("lab_number_format")
        policy = self.get("lab_reset_policy")
        today = date.today()
        pkey = self._period_key(policy, today)
        counter_key = f"lab::{fmt}::{pkey}"
        seq = self._next_seq(counter_key)
        return self.render(fmt, seq, today)

    # ── explicit "start from N" override ──
    def set_next_sequence(self, kind: str, next_value: int) -> dict:
        """
        Sets the counter so the NEXT generated number for `kind`
        ('patient' or 'lab') will be exactly `next_value`. Computes the
        SAME counter key next_patient_number()/next_lab_number() use
        internally — this only affects the currently-active format +
        currently-active reset period; changing either later starts a
        fresh counter at 1, same as it always has.
        """
        if kind not in ("patient", "lab"):
            raise ValueError("kind must be 'patient' or 'lab'")
        if next_value < 1:
            raise ValueError("next_value must be at least 1")

        fmt = self.get(f"{kind}_number_format")
        policy = self.get(f"{kind}_reset_policy")
        today = date.today()
        pkey = self._period_key(policy, today)
        counter_key = f"{kind}::{fmt}::{pkey}"
        full_key = f"seq::{counter_key}"

        # _next_seq() always increments before returning, so store one below
        # the target so the very next call yields next_value.
        stored = next_value - 1
        row = self.db.query(SystemConfig).filter(SystemConfig.key == full_key).first()
        if row:
            row.value = str(stored)
        else:
            row = SystemConfig(key=full_key, value=str(stored))
            self.db.add(row)
        self.db.commit()

        return {
            "kind": kind,
            "next_value": next_value,
            "preview": self.render(fmt, next_value, today),
        }