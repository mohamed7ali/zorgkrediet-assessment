# Zorgkrediet Intake & Assessment Prototype

An API-first FastAPI prototype of the online intake and affordability assessment for medical-treatment credit.

**This is a prototype, not a lending system.** It demonstrates the flow, makes decisions traceable, and identifies what a real implementation must still resolve (see [Questions to Raise Before a Real Build](#questions-to-raise-before-a-real-build) and [Known Limitations](#known-limitations)).

---

## Run It

```bash
git clone https://github.com/mohamed7ali/zorgkrediet-assessment.git && cd zorgkrediet
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Employee credentials (key:employee_id)
export EMPLOYEE_API_KEYS="demo-key-1:emp-001,demo-key-2:emp-002"

# Optional: real context summaries via Gemini.
# Without a key, a stub is used and free text is always referred to a human.
export GEMINI_API_KEY="..."
# export GEMINI_MODEL="gemini-2.5-flash"  # Optional override

uvicorn app:app --reload
```

- **Swagger UI:** http://127.0.0.1:8000/docs
- **Ready-to-run requests:** `requests.http` (VS Code REST Client / JetBrains HTTP Client)
- **Configuration:** `.env.example` lists every setting.

**Run tests:**

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Configuration Settings

| Setting | Purpose |
|---|---|
| `DATABASE_PATH` | SQLite file path (default: `./zorgkrediet.sqlite3`). |
| `EMPLOYEE_API_KEYS` | Comma-separated `key:employee_id` pairs. If unset, employee endpoints return `503` (fail closed). |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | Optional. Default model: `gemini-2.5-flash`. If unset, the application falls back to a stub that escalates to human review. |

## API Overview

| Method & Path | Who | What |
|---|---|---|
| `POST /applications` | Applicant | Submit the intake. Returns status, instalment, total, full schedule, and message. |
| `GET /applications/{id}` | Applicant | Returns the applicant view. The unguessable ID is the only credential (prototype only). |
| `GET /applications/{id}/dossier` | Employee (`X-API-Key`) | Returns questions asked, answers, rule basis, terms considered, summary/flag, and every audit event. |
| `POST /applications/{id}/override` | Employee (`X-API-Key`) | Accepts `{"outcome": "accepted\|referred\|declined", "reason": "..."}`. Employee ID comes from the key, never the body. |
| `GET /applications/{id}/audit/verify` | Employee (`X-API-Key`) | Recomputes the hash chain and cross-checks the current status. |
| `GET /health` | Anyone | Readiness probe that checks whether the database schema exists. |

## How a Decision Is Made

### Affordability Rule

```text
available room = net income − housing − existing obligations − household norm
```

| Household Type | Monthly Norm |
|---|---:|
| Single | €1,150 |
| Single + children | €1,400 |
| Couple | €1,600 |
| Couple + children | €1,850 |

**Decision rules:**

- **Accepted:** Instalment ≤ available room.
- **Referred:** Instalment > available room, and the excess is ≤ 10% of the available room (the room must be positive).
- **Declined:** Otherwise.

### Term Search

The preferred term is a preference. A longer term can only lower the instalment, so the system searches from the preferred term up to 60 months and selects the shortest term that is accepted.

- If no term is accepted, it selects the shortest term that is referred.
- If no term is referred either, the application is declined, showing the preferred term.
- Shorter terms are never tried.

**Example:** €3,000 requested, €200/month available room, 12-month preferred term.

| Term | Monthly Instalment | Outcome |
|---:|---:|---|
| 12 months | €262.22 | Declined |
| 13 months | €242.93 | Declined |
| 14 months | €226.40 | Declined |
| 15 months | €212.07 | Referred |
| 16 months | €199.54 | Accepted |

The applicant is told why the term changed, and every term tried is stored in the decision basis.

## Free-Text Triage

One model call (Gemini) turns the applicant's answer into a two-sentence financial summary and a `human_review` flag.

- The model **never makes the lending decision**.
- The flag can only change an accepted outcome into a referred outcome.
- An unavailable, blocked, malformed, or unconfigured model triggers human review.
- The prompt is in `app.py` (`CONTEXT_SYSTEM_PROMPT`, versioned).
- Applicant text is wrapped in `<applicant_text>` tags and treated as untrusted.
- Raw applicant text is not stored, returned, or audited.

The dossier retains this metadata alongside the summary:

```json
{
  "asked": true,
  "provided": true,
  "raw_text_retained": false
}
```

## Repayment Schedule

- **Nominal annual interest rate:** 8.9%, fixed.
- **Monthly interest rate:** 8.9% / 12.
- **Calculation method:** Standard annuity.
- **Money representation:** Integer cents / `Decimal`.

Interest is rounded to cents each month. The final instalment settles the remaining balance exactly, ensuring that instalments sum to principal plus interest and the closing balance is `€0.00`.

This is enforced by a real check that raises an exception on mismatch, rather than an `assert`, and is tested for every term from 6 to 60 months across several amounts.

**Worked example:** €1,000 over 3 months.

- Monthly instalment: €338.29.
- Interest by month: €7.42 + €4.96 + €2.49 = €14.87.
- Total repaid: €1,014.87.

## Audit Trail and Tamper Evidence

Each assessment writes an `application_assessed` event containing:

- The exact questions asked and answers.
- The rules and thresholds applied.
- Every term considered.
- The outcome and repayment schedule.
- The summary.
- The context source, model, and prompt version.
- The exact applicant message text and version served.

Overrides write `employee_override` events containing the employee, old and new outcomes, reason, and message.

### Hash Chain

SQLite triggers reject `UPDATE` and `DELETE` operations on `audit_events`.

Each event stores `previous_hash` and calculates:

```text
event_hash = SHA-256(previous_hash | timestamp | event_type | application_id | canonical_payload)
```

Canonical payloads use sorted keys, `(",", ":")` separators, and `ensure_ascii=False`.

A compliance officer can independently recompute the chain from the dossier JSON.

The `/audit/verify` endpoint:

1. Checks the integrity of the hash chain.
2. Verifies that the mutable `applications.outcome` matches the last audited decision.

A direct edit to the application outcome is therefore detected.

### What This Does Not Prove

Someone with full access to the database file can rewrite every event and recompute every hash.

The chain is also maintained per application. Deleting the newest event is detected only if that event changed the outcome and the status cross-check exposes the discrepancy. Deletion is not generally detectable.

To make the audit trail demonstrable to a regulator, I would:

1. Use one global chain across all applications.
2. Publish the latest `chain_head` outside the database's control at least daily, ideally after every event. Options include:
   - An RFC 3161 trusted timestamp.
   - A write-once store, such as S3 Object Lock in compliance mode in a separate account.
   - A signature generated with a key held in a KMS that the application cannot export.
3. Provide regulators with an export and documented verification procedure to recompute hashes and compare the chain head with the anchored value.
4. On PostgreSQL, deny the application role `UPDATE` and `DELETE` permissions on the audit table and serialise audit writes with a lock to prevent concurrent writers from forking the chain.

## The Decline Message

> We’re sorry, but based on the information provided, we’re unable to approve this application. This decision is about the monthly affordability of the repayments, not about how important your treatment is. We also checked longer repayment terms, up to 60 months, and none of them fit within your current monthly budget. If anything we used is inaccurate, or your circumstances change, please contact us. We can explain the decision, look at your figures again, and talk through what options may be available.

This message:

- Clearly communicates the decision.
- Separates the lending decision from the importance of the treatment.
- Explains that alternative repayment terms were considered.
- Offers a human next step.

It still needs legal and user-research review, including a Dutch version, required decision reasons, and contact details.

Applicants whose cases are referred receive a short message explaining that an employee will review the application first.

## Five Key Decisions

### 1. FastAPI + SQLite, API Only

**Why:** The quickest approach to run and review, allowing more time for the decision logic, repayment schedule, and audit evidence.

**Trade-offs:** Concurrency, production database operations, a user interface, and real authentication were sacrificed. A static API key stands in for employee login.

### 2. Integer Cents and a Final-Payment Adjustment

**Why:** No floating-point arithmetic is used for money, and the repayment schedule closes to exactly `€0.00`.

**Trade-offs:** The final instalment may differ by a few cents from the others, sacrificing a purely uniform annuity. Finance must approve this rounding convention.

### 3. Search Terms Upward and Prefer a Clean Acceptance

**Why:** Rather than showing the applicant a wall of failed options, the system automatically selects the shortest term that fits. Human involvement is required only when no term is accepted.

**Trade-offs:** When a referred term exists, the system does not choose the shortest possible term. The applicant may commit to more months and pay more total interest. This policy interpretation needs confirmation with Tim.

### 4. Hash-Chained, Append-Only SQLite Audit + Status Cross-Check

**Why:** Provides inexpensive, testable tamper evidence and a verification endpoint.

**Trade-offs:** It cannot prove integrity against a privileged full rewrite without external anchoring. The append-only design also raises the privacy question discussed below.

### 5. One Model Call That Can Only Escalate

**Why:** Minimises retained medical detail, keeps the model out of the lending decision, and fails safely to human review.

**Trade-offs:** The system depends on a third-party processor and cannot fully prevent prompt injection from influencing the review flag. A crafted answer could make the model return `human_review: false`.

The prompt attempts to limit this risk, and the code ensures that the model cannot improve an outcome. However, the risk cannot be fully eliminated.

## Questions to Raise Before a Real Build

### 1. Immutable Log vs. Privacy

The model summary is stored in an append-only table. If it contains medical details despite the prompt, those details cannot be removed.

- How do the seven-year retention rule and GDPR erasure/minimisation requirements interact?
- Should the summary be checked or constrained before storage?

### 2. Measurement Edge Cases

- Exactly how should the rule for being 10% over the available room be measured when the room is zero or negative, or when values fall on a rounding boundary?
- Is a referred term an acceptable alternative to offer?
- Is the shortest accepted term the right choice, given the additional interest it may entail?

### 3. Data Verification

Income and existing obligations are self-declared.

- Is verification through payslips or open banking in scope?
- Is a credit-registry (BKR) check required?
- What does the AFM expect for affordability assessments and household norms?
- How often do those norms change?

### 4. Gemini Vendor Terms

- Which vendor terms apply regarding training on inputs, retention, EU data regions, and the data processing agreement (DPA)?
- Free API tiers may use inputs to improve products. This must be checked before sending real applicant data.
- A Data Protection Impact Assessment (DPIA) and applicant notice are needed.

### 5. Authentication and Roles

- Employee SSO and role-based access.
- Separation of duties: who may override a decision, and can someone override their own referral?
- Applicant identity verification.
- Rate limiting, since every submission can trigger a paid model call.

### 6. Dossier Lifecycle

- How is a loan recorded as closed?
- When does the retention clock start?
- How are legal holds, backups, deletion, and evidence of deletion handled?

### 7. Decline and Referral Wording

- Legal review and Dutch translation.
- Required reasons and appeal route.
- Whether applicants should see the affordability figures behind a decline.

### 8. Audit Anchoring Choice

- Key ownership.
- Tamper alerting.
- Regulator export format.

## Added Beyond the Brief

The audit verification endpoint (`/audit/verify`) allows a compliance officer to test integrity directly rather than relying on a claim. It also cross-checks the mutable status against the last audited decision.

Smaller additions include:

- Separate applicant and employee views.
- An explanation when the repayment term changes.
- Continuous integration (CI).


## Known Limitations

The prototype does not include:

- A user interface.
- Search or multi-application views.
- Real authentication.
- Encryption at rest.
- A retention/deletion job.
- A loan-closure state.
- An external audit anchor.

Additional limitations:

- The applicant endpoint is protected only by an unguessable ID.
- Employee API keys are static.
- The live Gemini call was not exercised against the real service during development because no API key or network access was available. Request/response handling is tested against a fake HTTP layer.
- Before relying on Gemini, run a real request and verify the model name.
- The generated summary may occasionally echo sensitive details.

## What I Would Do Next

1. Implement a global audit chain with external anchoring.
2. Add employee SSO and role-based access control.
3. Implement applicant authentication.
4. Migrate to PostgreSQL with restricted database roles.
5. Build the retention and loan-closure lifecycle.
6. Review Dutch decline/referral wording.
7. Design BKR and income-verification processes.
8. Add rate limiting.
9. Build an applicant intake interface and an employee review screen.
