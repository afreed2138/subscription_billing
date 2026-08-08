# Copyright (c) 2026, Entries and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, today

from subscription_billing.date_utils import next_cycle_on_or_after, period_start

BILLABLE_STATUSES = ("Active",)
TERMINAL_STATUSES = ("Cancelled", "Expired")

ALLOWED_TRANSITIONS = {
	"Draft": {"Active", "Cancelled", "Expired"},
	"Active": {"Suspended", "Cancelled", "Expired"},
	"Suspended": {"Active", "Cancelled", "Expired"},
	"Cancelled": set(),
	"Expired": set(),
}

# Changing any of these after invoicing has begun would silently rewrite history.
SCHEDULE_DEFINING_FIELDS = ("subscription_plan", "billing_frequency", "start_date")


class CustomerSubscription(Document):
	def validate(self):
		self.set_default_company()
		self.validate_schedule_immutability()
		self.apply_plan_defaults()
		self.validate_dates()
		self.validate_amount()
		self.validate_status_transition()
		self.validate_cancellation_details()
		self.sync_billing_schedule()

	def set_default_company(self):
		"""Fall back to the session's default company rather than hard-coding one."""
		if not self.company:
			self.company = frappe.defaults.get_user_default("Company")

	def validate_schedule_immutability(self):
		"""Once a cycle has been invoiced, the schedule's inputs are frozen."""
		if self.is_new() or not (self.billing_cycles_completed or 0):
			return
		for field in SCHEDULE_DEFINING_FIELDS:
			if self.has_value_changed(field):
				frappe.throw(
					_("{0} cannot be changed after billing has started. Cancel this subscription and create a new one instead.").format(
						frappe.bold(_(self.meta.get_label(field)))
					),
					title=_("Schedule Locked"),
				)

	def apply_plan_defaults(self):
		"""Snapshot frequency (and price, if unset) from the plan onto the subscription."""
		if not self.subscription_plan:
			return
		if not (self.is_new() or self.has_value_changed("subscription_plan")):
			return
		plan = frappe.get_cached_doc("Customer Subscription Plan", self.subscription_plan)
		self.billing_frequency = plan.billing_frequency
		# Only fill a blank amount. An explicit 0 must reach validate_amount and fail.
		if self.get("amount") in (None, ""):
			self.amount = plan.price

	def validate_dates(self):
		"""End Date must be strictly after Start Date — a zero-length period has no cycle."""
		if getdate(self.start_date) >= getdate(self.end_date):
			frappe.throw(
				_("End Date ({0}) must be after Start Date ({1}).").format(
					frappe.format(self.end_date, "Date"), frappe.format(self.start_date, "Date")
				),
				title=_("Invalid Period"),
			)

	def validate_amount(self):
		if flt(self.amount) <= 0:
			frappe.throw(_("Amount must be greater than zero."), title=_("Invalid Amount"))

	def validate_status_transition(self):
		previous = self.get_doc_before_save()

		if not previous:
			if self.status not in ("Draft", "Active"):
				frappe.throw(
					_("A new subscription must start as Draft or Active, not {0}.").format(
						frappe.bold(_(self.status))
					)
				)
			if self.status == "Active":
				self.validate_activation()
			return

		if previous.status == self.status:
			return

		if self.status not in ALLOWED_TRANSITIONS.get(previous.status, set()):
			frappe.throw(
				_("Cannot change status from {0} to {1}.").format(
					frappe.bold(_(previous.status)), frappe.bold(_(self.status))
				),
				title=_("Invalid Status Change"),
			)

		if self.status == "Active":
			self.validate_activation()
			if previous.status == "Suspended":
				self.realign_schedule_after_resume()

	def validate_activation(self):
		"""A subscription may only go Active on a live plan and inside its own period."""
		if not frappe.db.get_value("Customer Subscription Plan", self.subscription_plan, "is_active"):
			frappe.throw(
				_("Subscription Plan {0} is not Active, so this subscription cannot be activated.").format(
					frappe.bold(self.subscription_plan)
				),
				title=_("Inactive Plan"),
			)
		if getdate(self.end_date) < getdate(today()):
			frappe.throw(
				_("End Date has already passed. Activate is not allowed — this subscription is Expired."),
				title=_("Period Over"),
			)

	def realign_schedule_after_resume(self):
		"""Skip cycles that elapsed while suspended so the customer is not back-billed."""
		target = next_cycle_on_or_after(self.start_date, self.billing_frequency, today())
		if target > (self.billing_cycles_completed or 0):
			skipped = target - self.billing_cycles_completed
			self.billing_cycles_completed = target
			frappe.msgprint(
				_("{0} billing cycle(s) skipped for the suspended period.").format(skipped),
				indicator="blue",
				alert=True,
			)

	def validate_cancellation_details(self):
		if self.status != "Cancelled":
			self.cancellation_date = None
			self.cancellation_reason = None
			return
		if not self.cancellation_date:
			frappe.throw(
				_("Cancellation Date is mandatory when a subscription is Cancelled."),
				title=_("Cancellation Incomplete"),
			)
		if not (self.cancellation_reason or "").strip():
			frappe.throw(
				_("Cancellation Reason is mandatory when a subscription is Cancelled."),
				title=_("Cancellation Incomplete"),
			)
		if getdate(self.cancellation_date) < getdate(self.start_date):
			frappe.throw(_("Cancellation Date cannot be before Start Date."))

	def sync_billing_schedule(self):
		"""Derive Next Billing Date from Start Date + cycles completed, never incrementally."""
		if self.status in TERMINAL_STATUSES:
			self.next_billing_date = None
			return
		upcoming = period_start(
			self.start_date, self.billing_frequency, self.billing_cycles_completed or 0
		)
		self.next_billing_date = upcoming if upcoming <= getdate(self.end_date) else None

	def is_billable(self):
		"""True when the scheduler is allowed to invoice this subscription."""
		return self.status in BILLABLE_STATUSES and bool(self.next_billing_date)

	def get_plan_item(self):
		return frappe.db.get_value("Customer Subscription Plan", self.subscription_plan, "item")
