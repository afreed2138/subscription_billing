"""Recurring billing engine.

Ordering matters: bill first, expire second. A subscription whose End Date has
passed may still owe an older cycle if the scheduler was down, and expiring it
first would silently drop that invoice.
"""

import frappe
from frappe import _
from frappe.utils import cstr, flt, getdate, today

from subscription_billing.date_utils import period_end, period_key, period_start

SAVEPOINT = "sb_cycle"

# A mistyped Start Date years in the past should not emit hundreds of invoices at once.
MAX_CYCLES_PER_RUN = 24

OPEN_STATUSES = ("Draft", "Active", "Suspended")


def run_billing(as_of=None, subscription=None):
	"""Bill every cycle due on or before `as_of`. One failure never stops the batch."""
	as_of = getdate(as_of or today())
	names = get_due_subscriptions(as_of, subscription)

	summary = {
		"as_of": cstr(as_of),
		"considered": len(names),
		"invoiced": 0,
		"skipped": 0,
		"failed": 0,
		"invoices": [],
		"errors": {},
	}

	for name in names:
		try:
			frappe.db.savepoint(SAVEPOINT)
			for outcome in bill_subscription(name, as_of):
				if outcome["status"] == "invoiced":
					summary["invoiced"] += 1
					summary["invoices"].append(outcome["sales_invoice"])
				else:
					summary["skipped"] += 1
			frappe.db.commit()
		except frappe.DuplicateEntryError:
			# Another worker claimed this cycle first: correct behaviour, not a failure.
			frappe.db.rollback(save_point=SAVEPOINT)
			summary["skipped"] += 1
		except Exception:
			frappe.db.rollback(save_point=SAVEPOINT)
			message = frappe.get_traceback(with_context=True)
			summary["failed"] += 1
			summary["errors"][name] = message.strip().splitlines()[-1][:200]
			record_failure(name, message)
			frappe.db.commit()

	return summary


def get_due_subscriptions(as_of, subscription=None):
	"""One indexed query returning names only — no document loads during the scan."""
	filters = {"status": "Active", "next_billing_date": ("<=", as_of)}
	if subscription:
		filters["name"] = subscription
	return frappe.get_all(
		"Customer Subscription", filters=filters, pluck="name", order_by="next_billing_date asc"
	)


def bill_subscription(name, as_of):
	"""Bill each due cycle of one subscription, oldest first, catching up if runs were missed."""
	sub = frappe.get_doc("Customer Subscription", name)
	outcomes = []
	for _ in range(MAX_CYCLES_PER_RUN):
		if not sub.is_billable() or getdate(sub.next_billing_date) > as_of:
			break
		outcomes.append(bill_one_cycle(sub))
	return outcomes


def bill_one_cycle(sub):
	"""Invoice the subscription's current cycle and roll the schedule forward."""
	cycle = sub.billing_cycles_completed or 0
	billing_date = getdate(sub.next_billing_date)

	log = upsert_cycle_log(sub, cycle, status="Pending")
	if log is None:
		# Already invoiced; realign the counter so the catch-up loop makes progress.
		advance_subscription(sub, billing_date)
		return {"cycle": cycle, "status": "skipped"}

	invoice = create_sales_invoice(sub, log, billing_date)
	log.sales_invoice = invoice.name
	log.status = "Invoiced"
	log.save(ignore_permissions=True)

	advance_subscription(sub, billing_date)
	return {"cycle": cycle, "status": "invoiced", "sales_invoice": invoice.name, "log": log.name}


def upsert_cycle_log(sub, cycle, status, error_message=None):
	"""Create or refresh this cycle's log. Returns None when the cycle is already Invoiced.

	The log's name is `{subscription}-{period}`, so the primary key — not a
	read-then-write check — is what makes double billing impossible.
	"""
	start = period_start(sub.start_date, sub.billing_frequency, cycle)
	key = period_key(start, sub.billing_frequency)
	name = f"{sub.name}-{key}"

	current_status = frappe.db.get_value("Subscription Billing Log", name, "status")
	if current_status == "Invoiced":
		return None

	if current_status:
		log = frappe.get_doc("Subscription Billing Log", name)
	else:
		log = frappe.new_doc("Subscription Billing Log")
		log.subscription = sub.name
		log.billing_period = key

	log.update(
		{
			"period_start_date": start,
			"period_end_date": period_end(sub.start_date, sub.billing_frequency, cycle),
			"billing_date": start,
			"amount": flt(sub.amount),
			"status": status,
			"error_message": error_message,
		}
	)
	log.save(ignore_permissions=True)
	return log


def create_sales_invoice(sub, log, billing_date):
	"""Build a standard ERPNext Sales Invoice through the document API, so its own validations run."""
	invoice = frappe.new_doc("Sales Invoice")
	invoice.customer = sub.customer
	invoice.company = sub.company
	invoice.set_posting_time = 1
	invoice.posting_date = billing_date
	invoice.customer_subscription = sub.name
	invoice.subscription_billing_log = log.name
	invoice.remarks = _("Subscription {0} — billing period {1} ({2} to {3})").format(
		sub.name,
		log.billing_period,
		frappe.format(log.period_start_date, "Date"),
		frappe.format(log.period_end_date, "Date"),
	)
	invoice.append(
		"items",
		{
			"item_code": sub.get_plan_item(),
			"qty": 1,
			"rate": flt(sub.amount),
		},
	)
	invoice.insert(ignore_permissions=True)
	invoice.submit()
	return invoice


def advance_subscription(sub, billed_on):
	"""Move the counter forward one cycle and expire the subscription if none remain."""
	sub.billing_cycles_completed = (sub.billing_cycles_completed or 0) + 1
	sub.last_billing_date = billed_on
	upcoming = period_start(sub.start_date, sub.billing_frequency, sub.billing_cycles_completed)
	if upcoming > getdate(sub.end_date):
		sub.status = "Expired"
	sub.save(ignore_permissions=True)


def record_failure(name, message):
	"""Persist the failure after its transaction was rolled back, so the cycle can be retried.

	The log is written as Failed rather than Invoiced, which leaves the name
	reusable — the next run picks the cycle up again.
	"""
	frappe.log_error(
		title=f"Subscription billing failed: {name}",
		message=message,
		reference_doctype="Customer Subscription",
		reference_name=name,
	)
	try:
		sub = frappe.get_doc("Customer Subscription", name)
		if sub.next_billing_date:
			upsert_cycle_log(
				sub,
				sub.billing_cycles_completed or 0,
				status="Failed",
				error_message=message[-1000:],
			)
	except Exception:
		frappe.log_error(title=f"Could not record billing failure for {name}")


def expire_subscriptions(as_of=None):
	"""Mark subscriptions whose End Date has passed as Expired."""
	as_of = getdate(as_of or today())
	names = frappe.get_all(
		"Customer Subscription",
		filters={"status": ("in", OPEN_STATUSES), "end_date": ("<", as_of)},
		pluck="name",
	)

	expired = []
	for name in names:
		try:
			frappe.db.savepoint(SAVEPOINT)
			sub = frappe.get_doc("Customer Subscription", name)
			sub.status = "Expired"
			sub.save(ignore_permissions=True)
			frappe.db.commit()
			expired.append(name)
		except Exception:
			frappe.db.rollback(save_point=SAVEPOINT)
			frappe.log_error(
				title=f"Could not expire subscription {name}",
				message=frappe.get_traceback(with_context=True),
				reference_doctype="Customer Subscription",
				reference_name=name,
			)
			frappe.db.commit()

	return expired
