# Subscription Billing

Subscription management and recurring billing for ERPNext. Customers are placed on a
plan, and a daily scheduled job raises real ERPNext Sales Invoices for each billing
cycle as it falls due — exactly once, with correct month-end date arithmetic.

Built and tested on **Frappe v16.12 / ERPNext v16.11 / Python 3.14**.

```
Customer → Subscription Plan → Subscription → billing due → Sales Invoice → Billing Log → next billing date
```

---

## 1. Installation

Requires an existing site with ERPNext installed.

```bash
cd frappe-bench
bench get-app subscription_billing /path/to/subscription_billing
bench --site <your-site> install-app subscription_billing
bench --site <your-site> migrate
```

Installing runs `after_install`, which is idempotent and also re-runs on every
`migrate`. It creates:

- Roles: `Subscription Manager`, `Billing User`, `Read Only User`
- Two read-only Custom Fields on Sales Invoice: `customer_subscription`,
  `subscription_billing_log`

Enable the scheduler so billing actually runs:

```bash
bench --site <your-site> enable-scheduler
```

> **Restarting after install:** if `bench start` was already running when the app was
> installed, restart it. Editable installs register via a `.pth` file that a live
> Python process never re-reads, so you would otherwise get
> `ModuleNotFoundError: No module named 'subscription_billing'`.

---

## 2. DocType structure

### Customer Subscription Plan

The reusable service master. Named by `plan_name`.

| Field | Type | Rule |
|---|---|---|
| Plan Name | Data | Mandatory, unique (document name) |
| Item | Link → Item | Mandatory; must be enabled and a Sales Item |
| Billing Frequency | Select | `Monthly` / `Yearly` |
| Price | Currency | Must be greater than zero |
| Description | Small Text | Optional |
| Active | Check | Default yes |

### Customer Subscription

One customer's subscription to a plan. Named `SUB-.####`.

| Field | Type | Rule |
|---|---|---|
| Customer | Link → Customer | Mandatory |
| Company | Link → Company | Mandatory; defaults to the session's default company |
| Subscription Plan | Link → Customer Subscription Plan | Mandatory |
| Billing Frequency | Select | Copied from the plan, read-only |
| Amount | Currency | Defaults to plan price, must be greater than zero |
| Status | Select | `Draft` / `Active` / `Suspended` / `Cancelled` / `Expired` |
| Start Date / End Date | Date | End Date must be strictly after Start Date |
| Billing Cycles Completed | Int | System maintained — the billing counter |
| Last Billing Date | Date | System maintained |
| Next Billing Date | Date | **Derived**, never stored incrementally |
| Cancellation Date / Reason | Date / Small Text | Both mandatory when Cancelled |

### Subscription Billing Log

One row per subscription per cycle. This is the ledger that makes billing idempotent.

| Field | Example |
|---|---|
| Subscription | `SUB-0001` |
| Billing Period | `2026-08` |
| Period Start / End Date | `2026-08-01` / `2026-08-31` |
| Billing Date | `2026-08-01` |
| Amount | `10,000` |
| Sales Invoice | `ACC-SINV-2026-00001` |
| Status | `Pending` / `Invoiced` / `Failed` |

---

## 3. Lifecycle

```
Draft ──► Active ⇄ Suspended
  │         │         │
  └────► Cancelled ◄──┘
            Expired
```

Transitions are enforced server-side by an explicit table (`ALLOWED_TRANSITIONS` in
`customer_subscription.py`). `Cancelled` and `Expired` are terminal. Activation is
refused if the plan is inactive or the End Date has already passed.

---

## 4. Billing calculation

**Every period is derived from the Start Date plus a cycle offset. Periods are never
computed by advancing the previous period, and never by adding 30 days.**

```python
period_start(anchor, frequency, cycle) = anchor + relativedelta(months=cycle * step)
# step = 1 for Monthly, 12 for Yearly
```

The subscription stores `billing_cycles_completed`; `next_billing_date` is recomputed
from the anchor on every save:

```python
next_billing_date = period_start(start_date, billing_frequency, billing_cycles_completed)
```

### Why this matters — month-end

Advancing incrementally (`next = add_months(next, 1)`) drifts downwards and never
recovers:

| Cycle | Incremental (wrong) | Anchor-based (this app) |
|---|---|---|
| 0 | 31 Jan | **31 Jan** |
| 1 | 28 Feb | **28 Feb** |
| 2 | 28 Mar ❌ | **31 Mar** ✅ |
| 3 | 28 Apr ❌ | **30 Apr** ✅ |
| 4 | 28 May ❌ | **31 May** ✅ |

The same property fixes leap years: an anchor of 29 Feb 2024 yields 28 Feb 2025,
28 Feb 2026, 28 Feb 2027 and then **29 Feb 2028** again.

A side benefit is that the schedule is self-correcting — the counter is the only
state, so no amount of replaying can shift the billing day.

### Catch-up

If the scheduler is down for a while, the next run bills every elapsed cycle in order,
one invoice each, capped at `MAX_CYCLES_PER_RUN = 24` so a mistyped Start Date years in
the past cannot emit hundreds of invoices in one go.

---

## 5. Duplicate prevention

**The Billing Log's document name *is* the idempotency key.**

```
autoname: format:{subscription}-{billing_period}   →   SUB-0001-2026-08
```

Because `name` is the primary key of `tabSubscription Billing Log`, uniqueness is
enforced by the database, not by application logic. The billing engine claims the log
row *before* it creates the invoice:

1. Insert the log for this cycle (`Pending`) — this claims the name
2. Create and submit the Sales Invoice
3. Mark the log `Invoiced` with the invoice reference
4. Advance the subscription counter
5. Commit

A concurrent second worker fails at step 1 with `DuplicateEntryError` and stops before
creating any invoice. This is why a read-then-write check (`if frappe.db.exists(...)`)
was rejected: two workers can both pass that check and both invoice.

Belt and braces: a fast `db.get_value` check short-circuits the common "already
invoiced" case so the exception path is only hit in a genuine race, and a Billing Log
that has a Sales Invoice cannot be deleted (`on_trash`).

Period keys are `%Y-%m` for monthly (`2026-08`) and `%Y` for yearly (`2026`) — computed
with `strftime`, so they never depend on the user's date-format preference.

---

## 6. Sales Invoice creation

Invoices are built through the document API (`frappe.new_doc` → `insert` → `submit`),
never by direct SQL, so all of ERPNext's own validation runs.

Each invoice carries:

- Customer and Company from the subscription
- The plan's Item, quantity `1`, subscription Amount as the rate
- `posting_date` = the cycle's billing date (with `set_posting_time = 1`)
- `customer_subscription` and `subscription_billing_log` back-references
- A `remarks` line naming the subscription and period covered

The back-reference fields are Custom Fields added on install. ERPNext's Sales Invoice
already has its own `subscription` field pointing at ERPNext's own Subscription
doctype, so ours are named distinctly and inserted after it.

---

## 7. Scheduled job

```python
scheduler_events = {"daily_long": ["subscription_billing.tasks.daily_billing"]}
```

`daily_billing` runs the billing sweep and then the expiry sweep. The long queue is
used because invoice generation is I/O heavy.

**Order matters: bill first, expire second.** A subscription whose End Date has just
passed may still owe an older cycle if the scheduler was down; expiring it first would
silently drop that invoice.

### Fault isolation

Each subscription is processed inside its own savepoint and committed independently:

```python
frappe.db.savepoint("sb_cycle")
try:    ... ; frappe.db.commit()
except: frappe.db.rollback(save_point="sb_cycle"); record_failure(...)
```

One subscription failing therefore cannot roll back or block the others. The failure is
written to the Error Log (linked to the subscription) *and* to that cycle's Billing Log
as `Failed`. Because a `Failed` log is not `Invoiced`, the cycle is automatically
retried on the next run.

### Efficiency

The scan is a single indexed query returning names only:

```python
frappe.get_all("Customer Subscription",
    filters={"status": "Active", "next_billing_date": ("<=", as_of)}, pluck="name")
```

`status` and `next_billing_date` both carry `search_index`. No documents are loaded
during the scan — only subscriptions that are actually due are ever fetched.

### Running it manually

```bash
bench --site <your-site> execute subscription_billing.tasks.daily_billing
```

Or from the Subscription form: **Subscription → Run Billing Now**.

---

## 8. Cancellation and suspension

| Action | Effect |
|---|---|
| **Suspend** | Status `Suspended`; excluded from the billing query, so no invoices |
| **Resume** | Schedule realigns forward — paused cycles are **skipped**, not back-billed |
| **Cancel** | Terminal; `next_billing_date` cleared. Requires date **and** reason |
| **Expire** | Set automatically when the End Date passes, or when no cycle fits before it |

Existing submitted invoices are never deleted or cancelled by this app.

**Resume policy.** On `Suspended → Active` the counter jumps to the first cycle
starting on or after today. Suspending on 10 Feb and resuming on 10 May (monthly,
anchored to the 1st) skips March, April and May, and the next invoice is 1 Jun.
Resuming exactly *on* a billing day still bills that day. This treats suspension as
"paused, not charged"; switch `realign_schedule_after_resume()` to a no-op if you want
catch-up billing instead.

---

## 9. Permissions and security

| Role | Plan | Subscription | Billing Log |
|---|---|---|---|
| Subscription Manager | full | full | read / report / export |
| Billing User | read / report / export | read / report / export | read / report / export |
| Accounts User | read | read | read |
| Read Only User | read | read | read |

- No role name is hard-coded anywhere in the logic. Permission checks are
  capability-based: `doc.check_permission("write")` and
  `frappe.has_permission("Sales Invoice", "create", throw=True)`.
- Triggering a billing run requires the right to *create Sales Invoices*, which is the
  real capability at stake. Grant `Billing User` alongside `Accounts User`.
- The engine writes with `ignore_permissions=True` **after** the API boundary has
  validated the caller. This is deliberate: billing is a system process, so a Billing
  User can trigger a run without holding write access to subscriptions themselves.
- All queries use the ORM. There is no raw SQL in the app.
- No Frappe or ERPNext core file is modified.

### Whitelisted API

| Method | Requires |
|---|---|
| `run_billing_now(subscription=None, as_of=None)` | read on Subscription + create on Sales Invoice |
| `activate_subscription(subscription)` | write on the document |
| `suspend_subscription(subscription)` | write on the document |
| `cancel_subscription(subscription, reason, cancellation_date=None)` | write on the document |
| `get_billing_preview(subscription, cycles=6)` | read on the document |

`as_of` is clamped to today, so the API cannot be used to post future-dated invoices.

---

## 10. Tests

```bash
bench --site <your-site> set-config allow_tests true
bench --site <your-site> run-tests --module subscription_billing.tests.test_subscription_billing
```

28 tests, all passing: 8 unit tests for the date arithmetic and 20 integration tests
that create and submit real Sales Invoices.

| # | Scenario | Test |
|---|---|---|
| 1 | Valid monthly subscription | `test_01_valid_monthly_subscription_gets_a_schedule` |
| 2 | Start Date > End Date | `test_02_start_date_on_or_after_end_date_is_rejected` |
| 3 | Inactive plan | `test_03_inactive_plan_blocks_activation` |
| 4 | Billing date reached | `test_04_due_billing_date_creates_a_sales_invoice` |
| 5 | Run billing twice | `test_05_running_billing_twice_creates_only_one_invoice` |
| 6 | Suspend subscription | `test_06_suspended_subscription_is_not_billed` |
| 7 | Cancel subscription | `test_07_cancelled_subscription_stops_future_billing` |
| 8 | End Date reached | `test_08_subscription_expires_once_end_date_passes` |
| 9 | Zero/negative amount | `test_09_zero_or_negative_amount_is_rejected` |
| 10 | Cancel without reason | `test_10_cancelling_without_date_or_reason_is_rejected` |
| 11 | Unauthorised API access | `test_11_unauthorised_api_access_is_denied` |
| 12 | One billing failure | `test_12_one_failing_subscription_does_not_stop_the_others` |

Plus: replay safety, DB-level unique key, catch-up billing, resume-skip, auto-expiry on
the final cycle, delete protection, schedule immutability and illegal transitions.

The suite suppresses `frappe.db.commit` for its duration. The engine commits per
subscription by design, and v16's `IntegrationTestCase` only rolls back at class
teardown — without this, tests would permanently write invoices into the site.

---

## 11. Technical decisions

**DocTypes are named `Customer Subscription` / `Customer Subscription Plan`.**
The assignment asks for `Subscription` and `Subscription Plan`, but ERPNext already
ships both of those names and DocType names are globally unique. Renaming was the only
option that keeps real Sales Invoice integration. `Subscription Billing Log` is
unchanged. Custom fields are named `customer_subscription` / `subscription_billing_log`
because Sales Invoice already has a `subscription` field.

**Subscription is not submittable.** The spec's Status field includes `Draft`, which
would collide with `docstatus`. Lifecycle is modelled with the Status field and an
explicit transition table instead.

**Frequency and amount are snapshotted onto the subscription** when the plan is
selected, not fetched live. Editing a plan's price must not silently reprice or
reschedule running subscriptions. Once a cycle has been invoiced, Plan, Billing
Frequency and Start Date are locked.

**An explicit `amount` of 0 is an error, not "unset".** Only a blank amount inherits
the plan price.

**`billing_cycles_completed` is the single source of truth.** `next_billing_date` is a
derived cache, so the schedule cannot drift out of sync with billing history.

---

## 12. Assumptions and limitations

- **Single currency.** The plan price is assumed to be in the customer's billing
  currency. Multi-currency subscriptions would need a Price List and an exchange-rate
  lookup at invoice time.
- **No proration.** Cancelling mid-cycle does not credit the unused portion, and
  suspension skips whole cycles only.
- **Overriding Amount on the same Item can clash with ERPNext's Item Price.** When
  Stock Settings has *"Auto insert Price List rate if missing"* enabled (the default),
  submitting an invoice creates an Item Price for that item. A second subscription that
  bills the *same item* at a different Amount then fails with
  `ItemPriceDuplicateItem`. Give each price point its own Plan and Item, or turn that
  setting off. The failure is isolated to that subscription and retried on the next run.
- **Billing in advance.** A cycle is invoiced on its first day and covers the period
  from that day to the day before the next cycle.
- **Taxes and discounts** are whatever ERPNext derives for the customer; the app sets
  no Tax Template of its own.
- **Deactivating a plan** blocks new activations but does not stop subscriptions that
  are already Active. A message warns how many are affected.
- **Invoice posting date** is the cycle's billing date, which can be in the past on a
  catch-up run. A closed accounting period would reject it; the failure is recorded and
  the cycle retried.
- **Catch-up is capped** at 24 cycles per subscription per run.
- The daily job assumes one run per day. Running it more often is safe (idempotent) but
  does not shorten cycles.
