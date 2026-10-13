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
CONTEXT_PROMPT_VERSION = "1"

# Wording shown to applicants. The exact text and its version are written into
# the audit trail, so a regulator can see what an applicant was told even after
# this copy is later edited.
APPLICANT_MESSAGES = {
    "declined": {
        "version": "declined-v2",
        "text": (
            "We're sorry, but based on the information provided, we're unable to approve "
            "this application. This decision is about the monthly affordability of the "
            "repayments, not about how important your treatment is. We also checked longer "
            "repayment terms, up to 60 months, and none of them fit within your current "
            "monthly budget. If anything we used is inaccurate, or your circumstances change, "
            "please contact us. We can explain the decision, look at your figures again, and "
            "talk through what options may be available."
        ),
    },
    "referred": {
        "version": "referred-v1",
        "text": (
            "Thank you for your application. We can't give you a final answer yet: one of our "
            "employees will review it first. We'll be in touch about the next step."
        ),
    },
}


def applicant_message(outcome: str) -> dict | None:
    return APPLICANT_MESSAGES.get(outcome)


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
        CREATE TABLE IF NOT EXISTS audit_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            previous_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL,
            FOREIGN KEY(application_id) REFERENCES applications(id)
        );
        CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
        """)


def _employee_keys() -> dict[str, str]:
    """EMPLOYEE_API_KEYS="key1:emp-001,key2:emp-002" — prototype stand-in for real SSO."""
    pairs = (
        item.split(":", 1)
        for item in os.getenv("EMPLOYEE_API_KEYS", "").split(",")
        if ":" in item
    )
    return {k.strip(): v.strip() for k, v in pairs if k.strip() and v.strip()}


def current_employee(x_api_key: str | None = Header(default=None)) -> str:
    keys = _employee_keys()
    if not keys:
        raise HTTPException(503, "Employee access is not configured (set EMPLOYEE_API_KEYS)")
    if x_api_key:
        for key, employee in keys.items():
            if hmac.compare_digest(key.encode(), x_api_key.encode()):
                return employee
    raise HTTPException(401, "Invalid or missing X-API-Key")


def _audit(c: sqlite3.Connection, app_id: str, event_type: str, payload: dict) -> None:
    previous = c.execute(
        "SELECT event_hash FROM audit_events WHERE application_id=? ORDER BY seq DESC LIMIT 1",
        (app_id,),
    ).fetchone()
    previous_hash = previous[0] if previous else "0" * 64
    timestamp = now_iso()
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    event_hash = hashlib.sha256(
        f"{previous_hash}|{timestamp}|{event_type}|{app_id}|{canonical}".encode()
    ).hexdigest()
    c.execute(
        "INSERT INTO audit_events"
        "(application_id,event_type,timestamp,payload_json,previous_hash,event_hash)"
        " VALUES(?,?,?,?,?,?)",
        (app_id, event_type, timestamp, canonical, previous_hash, event_hash),
    )


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
        audits = c.execute(
            "SELECT seq,event_type,timestamp,payload_json,previous_hash,event_hash "
            "FROM audit_events WHERE application_id=? ORDER BY seq",
            (application_id,),
        ).fetchall()
    return row, audits


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
        "applicant_message": (applicant_message(row["outcome"]) or {}).get("text"),
        "requested_term_months": requested,
        "term_note": note,
        "repayment": _repayment(row),
    }


def _dossier_view(row, audits) -> dict:
    """Employee/compliance view: the full record."""
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "answers": json.loads(row["request_json"]),
        "outcome": row["outcome"],
        "system_outcome": row["system_outcome"],
        "decision_basis": json.loads(row["decision_basis"]),
        "repayment": _repayment(row),
        "context_summary": row["note_summary"],
        "human_review_flag": bool(row["human_review_flag"]),
        "override_reason": row["final_reason"],
        "applicant_message": (applicant_message(row["outcome"]) or {}).get("text"),
        "audit_events": [
            {
                "seq": a["seq"],
                "event_type": a["event_type"],
                "timestamp": a["timestamp"],
                "previous_hash": a["previous_hash"],
                "event_hash": a["event_hash"],
                "payload": json.loads(a["payload_json"]),
            }
            for a in audits
        ],
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
        _audit(c, app_id, "application_assessed", {
            "created_at": created,
            "questionnaire": QUESTIONNAIRE,
            "answers": request_record,
            "system_outcome": system_outcome,
            "outcome_after_context_flag": outcome,
            "decision_basis": basis,
            "monthly_payment_cents": payment,
            "total_repaid_cents": total,
            "schedule": sched,
            "context_summary": context["summary"],
            "human_review_flag": context["human_review"],
            "context_source": context["source"],
            "context_model": context.get("model"),
            "context_prompt_version": CONTEXT_PROMPT_VERSION,
            "applicant_message": applicant_message(outcome),
        })

    return _applicant_view(_fetch(app_id)[0])


@app.get("/applications/{application_id}")
def get_application(application_id: str):
    return _applicant_view(_fetch(application_id)[0])


@app.get("/applications/{application_id}/dossier")
def get_dossier(application_id: str, _employee: str = Depends(current_employee)):
    return _dossier_view(*_fetch(application_id))


