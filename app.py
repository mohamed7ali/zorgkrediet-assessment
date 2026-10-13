"""Zorgkrediet intake & assessment API."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Literal
from urllib.request import Request, urlopen

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("zorgkrediet")
DB_PATH = Path(os.getenv("DATABASE_PATH", "./zorgkrediet.sqlite3"))
ANNUAL_RATE = Decimal("0.089")
MONTHLY_RATE = ANNUAL_RATE / Decimal(12)
NORMS = {
    "single": 115000,
    "single_with_children": 140000,
    "couple": 160000,
    "couple_with_children": 185000,
}
CENT = Decimal("0.01")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def money(pence: int) -> str:
    return f"{Decimal(pence) / 100:.2f}"


def cents(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def build_schedule(principal_cents: int, months: int) -> tuple[list[dict], int, int]:
    """Monthly fixed annuity; round each period to cents and settle the final cent exactly."""
    principal = Decimal(principal_cents) / 100
    regular = (
        principal * MONTHLY_RATE / (1 - (1 + MONTHLY_RATE) ** (-months))
    ).quantize(CENT, rounding=ROUND_HALF_UP)

    balance = principal
    rows = []
    total_interest = Decimal("0.00")

    for period in range(1, months + 1):
        interest = (balance * MONTHLY_RATE).quantize(CENT, rounding=ROUND_HALF_UP)
        payment = (
            (balance + interest).quantize(CENT)
            if period == months
            else min(regular, balance + interest)
        )
        principal_paid = payment - interest
        balance = (balance - principal_paid).quantize(CENT)
        total_interest += interest
        rows.append({
            "month": period,
            "payment_eur": f"{payment:.2f}",
            "principal_eur": f"{principal_paid:.2f}",
            "interest_eur": f"{interest:.2f}",
            "closing_balance_eur": f"{balance:.2f}",
        })

    if balance != 0 or sum(Decimal(r["payment_eur"]) for r in rows) != principal + total_interest:
        raise RuntimeError("Repayment schedule does not reconcile to the cent")

    return rows, cents(regular), cents(principal + total_interest)


def determine_outcome(payment: int, room: int) -> str:
    if payment <= room:
        return "accepted"
    if room > 0 and Decimal(payment - room) / Decimal(room) <= Decimal("0.10"):
        return "referred"
    return "declined"


class Intake(BaseModel):
    treatment: Literal["dental", "eye", "orthodontic", "other"]
    treatment_cost_eur: Decimal = Field(ge=500, le=25000)
    preferred_term_months: int = Field(ge=6, le=60)
    net_monthly_income_eur: Decimal = Field(ge=0)
    housing_costs_eur: Decimal = Field(ge=0)
    existing_obligations_eur: Decimal = Field(ge=0)
    household: Literal["single", "single_with_children", "couple", "couple_with_children"]
    additional_context: str = Field(default="", max_length=4000)

    @field_validator(
        "treatment_cost_eur",
        "net_monthly_income_eur",
        "housing_costs_eur",
        "existing_obligations_eur",
    )
    @classmethod
    def require_cent_precision(cls, value: Decimal) -> Decimal:
        if value.as_tuple().exponent < -2:
            raise ValueError("Amounts must be expressed to no more than two decimal places")
        return value


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx(immediate: bool = False):
    conn = _db()
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tx() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS applications (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            request_json TEXT NOT NULL,
            outcome TEXT NOT NULL,
            system_outcome TEXT NOT NULL,
            decision_basis TEXT NOT NULL,
            selected_term INTEGER NOT NULL,
            payment_cents INTEGER NOT NULL,
            total_repaid_cents INTEGER NOT NULL,
            schedule_json TEXT NOT NULL,
            note_summary TEXT NOT NULL,
            human_review_flag INTEGER NOT NULL,
            final_reason TEXT
        );
        """)


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(
    title="Zorgkrediet Intake & Assessment",
    version="0.2.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    with tx() as c:
        c.execute("SELECT 1 FROM applications LIMIT 1")
    return {"status": "ok"}


