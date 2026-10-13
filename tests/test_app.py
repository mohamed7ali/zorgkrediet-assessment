import json
from decimal import Decimal

from fastapi.testclient import TestClient

import pytest

import app as api

H = {"X-API-Key": "test-key-1"}


@pytest.fixture(autouse=True)
def employee_keys(monkeypatch):
    monkeypatch.setenv("EMPLOYEE_API_KEYS", "test-key-1:emp-1,test-key-2:emp-2")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def test_schedule_closes_exactly_and_sums_to_principal_plus_interest():
    rows, regular, total = api.build_schedule(500_000, 36)
    assert len(rows) == 36
    assert rows[-1]["closing_balance_eur"] == "0.00"
    assert sum(Decimal(row["payment_eur"]) for row in rows) == Decimal(total) / 100
    assert sum(Decimal(row["principal_eur"]) for row in rows) == Decimal("5000.00")
    assert regular > 0


def test_schedule_reconciles_for_every_amount_and_term():
    for principal in (50_000, 123_457, 999_999, 2_500_000):
        for months in range(6, 61):
            rows, regular, total = api.build_schedule(principal, months)
            assert rows[-1]["closing_balance_eur"] == "0.00"
            assert sum(Decimal(r["payment_eur"]) for r in rows) == Decimal(total) / 100
            assert sum(Decimal(r["principal_eur"]) for r in rows) == Decimal(principal) / 100


def test_shorter_terms_are_never_tried():
    # sanity check of the premise behind dropping the "shorter term" branch
    payments = [api.build_schedule(1_000_000, t)[1] for t in range(6, 61)]
    assert payments == sorted(payments, reverse=True)


