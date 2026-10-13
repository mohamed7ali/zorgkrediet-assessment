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


def _income_for_room(room_eur, housing=1000, obligations=200, norm=1150):
    return housing + obligations + norm + room_eur


def test_term_search_picks_shortest_accepted_even_when_an_earlier_term_is_only_referred(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "t1.sqlite3")
    _no_context(monkeypatch)
    with TestClient(api.app, headers=H) as client:
        res = client.post(
            "/applications",
            json=_body(
                treatment_cost_eur=3000,
                preferred_term_months=12,
                net_monthly_income_eur=_income_for_room(200),
            ),
        ).json()
        assert res["outcome"] == "accepted"
        chosen = res["repayment"]["term_months"]
        assert chosen > 12
    with api._db() as db:
        basis = json.loads(
            db.execute("SELECT decision_basis FROM applications").fetchone()[0]
        )
    considered = basis["terms_considered"]
    assert considered[0]["term_months"] == 12 and considered[0]["outcome"] == "declined"
    assert "referred" in [o["outcome"] for o in considered]
    assert [o["outcome"] for o in considered].count("accepted") == 1
    assert considered[-1]["term_months"] == chosen


def test_term_search_falls_back_to_first_referred_when_nothing_is_accepted(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "t2.sqlite3")
    _no_context(monkeypatch)
    _, p60, _ = api.build_schedule(500_000, 60)
    room_eur = Decimal(int(p60 * 0.95)) / 100   # room is 5% below the 60-month instalment
    with TestClient(api.app, headers=H) as client:
        res = client.post(
            "/applications",
            json=_body(
                treatment_cost_eur=5000,
                preferred_term_months=6,
                net_monthly_income_eur=float(_income_for_room(room_eur)),
            ),
        ).json()
        assert res["outcome"] == "referred"
    with api._db() as db:
        basis = json.loads(
            db.execute("SELECT decision_basis FROM applications").fetchone()[0]
        )
    outcomes = [o["outcome"] for o in basis["terms_considered"]]
    assert "accepted" not in outcomes
    first_referred = next(o for o in basis["terms_considered"] if o["outcome"] == "referred")
    assert basis["selected_term_months"] == first_referred["term_months"]


def test_term_note_explains_a_changed_term_to_the_applicant(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "n.sqlite3")
    _no_context(monkeypatch)
    with TestClient(api.app, headers=H) as client:
        res = client.post(
            "/applications",
            json=_body(
                treatment_cost_eur=3000,
                preferred_term_months=12,
                net_monthly_income_eur=_income_for_room(200),
            ),
        ).json()
        assert res["requested_term_months"] == 12
        assert res["repayment"]["term_months"] > 12
        assert "You asked for 12 months" in res["term_note"]


def test_term_extension_preferred_and_decline_message(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "term.sqlite3")
    _no_context(monkeypatch)
    with TestClient(api.app, headers=H) as client:
        body = {
            "treatment": "eye",
            "treatment_cost_eur": 25000,
            "preferred_term_months": 6,
            "net_monthly_income_eur": 3000,
            "housing_costs_eur": 1000,
            "existing_obligations_eur": 200,
            "household": "single",
            "additional_context": "",
        }
        response = client.post("/applications", json=body)
        assert response.status_code == 201
        dossier = response.json()
        assert dossier["repayment"]["term_months"] >= 6
        assert dossier["repayment"]["schedule"][-1]["closing_balance_eur"] == "0.00"
        body["net_monthly_income_eur"] = 0
        declined = client.post("/applications", json=body).json()
        assert declined["outcome"] == "declined"
        assert "unable to approve" in declined["applicant_message"]
        assert "up to 60 months" in declined["applicant_message"]


def test_application_audit_override_and_privacy(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(api, "summarize_context", lambda text: {
        "summary": "Temporary contract; review income stability.",
        "human_review": True,
        "source": "stub",
    })
    with TestClient(api.app, headers=H) as client:
        body = {
            "treatment": "dental",
            "treatment_cost_eur": 5000,
            "preferred_term_months": 36,
            "net_monthly_income_eur": 4000,
            "housing_costs_eur": 1000,
            "existing_obligations_eur": 200,
            "household": "single",
            "additional_context": "temporary contract and sensitive diagnosis",
        }
        created = client.post("/applications", json=body)
        assert created.status_code == 201, created.text
        applicant = created.json()
        app_id = applicant["id"]

        # Applicant view must not expose internal fields
        assert not {
            "audit_events", "decision_basis", "answers",
            "context_summary", "human_review_flag",
        } & set(applicant)

        dossier = client.get(f"/applications/{app_id}/dossier").json()
        assert "sensitive diagnosis" not in json.dumps(dossier)
        assert dossier["human_review_flag"] is True
        assert dossier["answers"]["additional_context"] == {
            "asked": True, "provided": True, "raw_text_retained": False,
        }
        assert "sensitive diagnosis" not in created.text
        assert (
            "Is there anything else we should know about your situation?"
            in str(dossier["audit_events"][0]["payload"])
        )
        assert dossier["repayment"]["schedule"][-1]["closing_balance_eur"] == "0.00"
        assert client.get(f"/applications/{app_id}/audit/verify").json()["valid"] is True

        overridden = client.post(
            f"/applications/{app_id}/override",
            json={"outcome": "accepted", "reason": "Verified new supporting evidence.",
                  "employee_id": "someone-else"},
        )
        assert overridden.status_code == 200
        assert overridden.json()["outcome"] == "accepted"
        assert len(overridden.json()["audit_events"]) == 2
        # acting employee comes from the credential, never from the request body
        assert overridden.json()["audit_events"][1]["payload"]["employee_id"] == "emp-1"
        assert client.get(f"/applications/{app_id}/audit/verify").json()["checked_events"] == 2

        with api._db() as db:
            try:
                db.execute(
                    "UPDATE audit_events SET payload_json='{}' WHERE application_id=?",
                    (app_id,),
                )
                assert False, "audit table should reject updates"
            except Exception as exc:
                assert "append-only" in str(exc)
            db.execute("DROP TRIGGER audit_no_update")
            db.execute(
                "UPDATE audit_events SET payload_json='{}' WHERE application_id=?",
                (app_id,),
            )
        assert client.get(f"/applications/{app_id}/audit/verify").json()["valid"] is False


def test_employee_endpoints_require_a_valid_key_and_applicant_view_is_open(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "a.sqlite3")
    _no_context(monkeypatch)
    with TestClient(api.app) as anon:           # no credential
        app_id = anon.post("/applications", json=_body()).json()["id"]
        assert anon.get(f"/applications/{app_id}").status_code == 200
        for method, url in (
            ("get", f"/applications/{app_id}/dossier"),
            ("get", f"/applications/{app_id}/audit/verify"),
        ):
            assert getattr(anon, method)(url).status_code == 401
        body = {"outcome": "accepted", "reason": "Because I said so."}
        assert anon.post(f"/applications/{app_id}/override", json=body).status_code == 401
        assert (
            anon.post(
                f"/applications/{app_id}/override",
                json=body,
                headers={"X-API-Key": "wrong"},
            ).status_code
            == 401
        )
        monkeypatch.delenv("EMPLOYEE_API_KEYS")
        assert (
            anon.get(f"/applications/{app_id}/dossier", headers=H).status_code == 503
        )

    
