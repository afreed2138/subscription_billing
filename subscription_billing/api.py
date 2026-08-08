"""Whitelisted endpoints.

Each one validates the caller's own permissions before doing anything. The engine
below then writes with ignore_permissions on purpose: billing is a system process,
so a Billing User can trigger a run without holding write access to subscriptions.
"""

import frappe
from frappe import _
from frappe.utils import cint, getdate, today

from subscription_billing import billing
from subscription_billing.date_utils import period_end, period_start


@frappe.whitelist()
def run_billing_now(subscription=None, as_of=None):
	"""Run the billing cycle on demand. Requires the right to raise Sales Invoices."""
	frappe.has_permission("Customer Subscription", "read", throw=True)
	frappe.has_permission("Sales Invoice", "create", throw=True)

	as_of = getdate(as_of or today())
	if as_of > getdate(today()):
		frappe.throw(_("Cannot run billing for a future date."))

	if subscription:
		frappe.get_doc("Customer Subscription", subscription).check_permission("read")

	return billing.run_billing(as_of=as_of, subscription=subscription)


@frappe.whitelist()
def activate_subscription(subscription):
	"""Draft or Suspended -> Active. Blocked if the plan is inactive."""
	return _set_status(subscription, "Active")


@frappe.whitelist()
def suspend_subscription(subscription):
	"""Active -> Suspended. The scheduler stops invoicing immediately."""
	return _set_status(subscription, "Suspended")


@frappe.whitelist()
def cancel_subscription(subscription, reason, cancellation_date=None):
	"""Cancel permanently. Future billing stops; submitted invoices are left alone."""
	if not (reason or "").strip():
		frappe.throw(_("Cancellation Reason is required."))
	return _set_status(
		subscription,
		"Cancelled",
		cancellation_reason=reason,
		cancellation_date=getdate(cancellation_date or today()),
	)


@frappe.whitelist()
def get_billing_preview(subscription, cycles=6):
	"""Upcoming billing dates — the quickest way to verify month-end behaviour."""
	doc = frappe.get_doc("Customer Subscription", subscription)
	doc.check_permission("read")

	first = doc.billing_cycles_completed or 0
	rows = []
	for cycle in range(first, first + min(cint(cycles) or 6, 24)):
		start = period_start(doc.start_date, doc.billing_frequency, cycle)
		if start > getdate(doc.end_date):
			break
		rows.append(
			{
				"cycle": cycle + 1,
				"billing_date": start,
				"period_end_date": period_end(doc.start_date, doc.billing_frequency, cycle),
				"amount": doc.amount,
			}
		)
	return rows


def _set_status(subscription, status, **extra):
	"""Shared status write — permission checked against the document, not a role name."""
	doc = frappe.get_doc("Customer Subscription", subscription)
	doc.check_permission("write")
	doc.status = status
	doc.update(extra)
	doc.save()
	return {
		"name": doc.name,
		"status": doc.status,
		"next_billing_date": doc.next_billing_date,
		"billing_cycles_completed": doc.billing_cycles_completed,
	}
