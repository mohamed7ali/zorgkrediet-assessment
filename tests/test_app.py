import json
from decimal import Decimal

from fastapi.testclient import TestClient

import pytest

import app as api

H = {"X-API-Key": "test-key-1"}

def _body(**overrides):
    body = {
        "treatment": "dental",
        "treatment_cost_eur": 5000,
        "preferred_term_months": 36,
        "net_monthly_income_eur": 4000,
        "housing_costs_eur": 1000,
        "existing_obligations_eur": 200,
        "household": "single",
        "additional_context": "",
    }
    body.update(overrides)
    return body


def _no_context(monkeypatch):
    monkeypatch.setattr(api, "summarize_context", lambda text: {
        "summary": "No additional context provided.",
        "human_review": False,
        "source": "empty",
    })

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


def test_affordability_boundaries():
    assert api.determine_outcome(10000, 10000) == "accepted"   # equal → accepted
    assert api.determine_outcome(11000, 10000) == "referred"   # exactly 10% over → referred
    assert api.determine_outcome(11001, 10000) == "declined"   # one cent over 10% → declined
    assert api.determine_outcome(1, 0) == "declined"           # zero room → declined


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-5", "10.123", "abc"])
def test_malformed_money_is_rejected_with_422(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "x.sqlite3")
    _no_context(monkeypatch)
    with TestClient(api.app, headers=H) as client:
        assert client.post("/applications", json=_body(net_monthly_income_eur=bad)).status_code == 422


def test_health_returns_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "h.sqlite3")
    with TestClient(api.app) as client:
        assert client.get("/health").json() == {"status": "ok"}


