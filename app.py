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


QUESTIONNAIRE = {
    "version": "1",
    "questions": [
        {"field": "treatment", "prompt": "Type of treatment",
         "choices": ["dental", "eye", "orthodontic", "other"]},
        {"field": "treatment_cost_eur", "prompt": "Cost of the treatment (€500–€25,000)", "unit": "EUR"},
        {"field": "preferred_term_months", "prompt": "Preferred repayment term in months (6–60)", "unit": "months"},
        {"field": "net_monthly_income_eur", "prompt": "Net monthly income", "unit": "EUR/month"},
        {"field": "housing_costs_eur", "prompt": "Monthly housing costs (rent or mortgage)", "unit": "EUR/month"},
        {"field": "existing_obligations_eur",
         "prompt": "Total monthly payments on existing loans and credit", "unit": "EUR/month"},
        {"field": "household", "prompt": "Household situation", "choices": list(NORMS)},
        {"field": "additional_context",
         "prompt": "Is there anything else we should know about your situation?",
         "retention": "raw answer discarded after assessment"},
    ],
}


def _fetch(application_id: str):
    with tx() as c:
        row = c.execute(
            "SELECT * FROM applications WHERE id=?", (application_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Application not found")
    return row, []


def _repayment(row) -> dict:
    return {
        "term_months": row["selected_term"],
        "monthly_payment_eur": money(row["payment_cents"]),
        "total_repaid_eur": money(row["total_repaid_cents"]),
        "schedule": json.loads(row["schedule_json"]),
    }


def _applicant_view(row) -> dict:
    requested = json.loads(row["request_json"])["preferred_term_months"]
    note = None
    if row["outcome"] != "declined" and row["selected_term"] != requested:
        note = (
            f"You asked for {requested} months. "
            f"A term of {row['selected_term']} months keeps the monthly "
            f"instalment within your budget, so that is what is shown."
        )
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "outcome": row["outcome"],
        "applicant_message": None,       # filled in chunk 7
        "requested_term_months": requested,
        "term_note": note,
        "repayment": _repayment(row),
    }


def summarize_context(text: str) -> dict:
    """Stub — replaced in chunk 13."""
    return {"summary": "", "human_review": False, "source": "empty", "model": None}


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


@app.post("/applications", status_code=201)
def create_application(data: Intake):
    requested_principal = cents(data.treatment_cost_eur)
    norm = NORMS[data.household]
    room = (
        cents(data.net_monthly_income_eur)
        - cents(data.housing_costs_eur)
        - cents(data.existing_obligations_eur)
        - norm
    )

    # Term search.
    # A longer term can only lower the instalment, so shorter terms than the
    # preference can never help and are not tried.
    # Walk from the preferred term up to 60 months:
    #   - take the shortest term that is *accepted*
    #   - if none, fall back to the shortest term that is *referred*
    #   - if none, decline (showing the preferred term)
    options = []
    selected = None
    fallback_referred = None

    for term in range(data.preferred_term_months, 61):
        sched, payment, total = build_schedule(requested_principal, term)
        outcome = determine_outcome(payment, room)
        options.append({"term_months": term, "payment_cents": payment, "outcome": outcome})
        if outcome == "accepted":
            selected = (term, sched, payment, total, outcome)
            break
        if outcome == "referred" and fallback_referred is None:
            fallback_referred = (term, sched, payment, total, outcome)

    if selected is None:
        selected = fallback_referred
    if selected is None:
        term = data.preferred_term_months
        sched, payment, total = build_schedule(requested_principal, term)
        outcome = "declined"
    else:
        term, sched, payment, total, outcome = selected

    system_outcome = outcome
    context = summarize_context(data.additional_context)
    if context["human_review"] and outcome == "accepted":
        outcome = "referred"

    request_record = data.model_dump(mode="json", exclude={"additional_context"})
    request_record["additional_context"] = {
        "asked": True,
        "provided": bool(data.additional_context.strip()),
        "raw_text_retained": False,
    }

    basis = {
        "rule": (
            "available room = net monthly income - housing costs "
            "- existing obligations - household living norm"
        ),
        "norm_cents": norm,
        "available_room_cents": room,
        "system_outcome_before_context_flag": system_outcome,
        "thresholds": {
            "accept": "payment <= available room",
            "refer": "excess <= 10% of positive available room",
            "decline": "otherwise",
        },
        "interest_rate_annual": "0.089",
        "preferred_term_months": data.preferred_term_months,
        "selected_term_months": term,
        "term_selection": (
            "terms from the preferred term up to 60 months are tried in order; "
            "shortest accepted term wins, else shortest referred term, "
            "else decline at the preferred term"
        ),
        "terms_considered": options,
        "context_review_flag": context["human_review"],
    }

    app_id = str(uuid.uuid4())
    created = now_iso()

    with tx() as c:
        c.execute(
            "INSERT INTO applications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                app_id, created,
                json.dumps(request_record, ensure_ascii=False),
                outcome, system_outcome,
                json.dumps(basis),
                term, payment, total,
                json.dumps(sched),
                context["summary"],
                int(context["human_review"]),
                None,
            ),
        )

    return _applicant_view(_fetch(app_id)[0])


@app.get("/applications/{application_id}")
def get_application(application_id: str):
    return _applicant_view(_fetch(application_id)[0])


