"""Zorgkrediet intake & assessment API."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

ANNUAL_RATE = Decimal("0.089")
MONTHLY_RATE = ANNUAL_RATE / Decimal(12)
NORMS = {
    "single": 115000,
    "single_with_children": 140000,
    "couple": 160000,
    "couple_with_children": 185000,
}
CENT = Decimal("0.01")


def cents(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money(pence: int) -> str:
    return f"{Decimal(pence) / 100:.2f}"


def build_schedule(principal_cents: int, months: int) -> tuple[list[dict], int, int]:
    """Monthly fixed annuity; round each period to cents and settle the final cent exactly.

    Returns (rows, regular_payment_cents, total_repaid_cents).
    Raises RuntimeError if the schedule does not reconcile — this is a real
    check, not an assert (asserts are stripped under python -O).
    """
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


