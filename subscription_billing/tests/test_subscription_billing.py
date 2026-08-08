"""The 12 mandatory scenarios from section 12 of the assignment.

Test names are numbered to match that table 1:1, with extra tests below them for
the date arithmetic and idempotency guarantees the 12 depend on.

This module deliberately lives outside a doctype folder: frappe would otherwise
auto-generate test records for every linked doctype (including a full Company
with a chart of accounts) and commit them to the site.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import add_days, flt, getdate

from subscription_billing import api, billing
from subscription_billing.date_utils import (
	next_cycle_on_or_after,
	period_end,
	period_key,
	period_start,
)


class TestBillingPeriods(UnitTestCase):
	"""Pure date arithmetic — no database involved."""

	def test_month_end_billing_day_does_not_drift(self):
		got = [str(period_start("2026-01-31", "Monthly", i)) for i in range(5)]
		self.assertEqual(
			got, ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30", "2026-05-31"]
		)

	def test_monthly_is_not_thirty_days(self):
		self.assertEqual(str(period_start("2026-02-01", "Monthly", 1)), "2026-03-01")

	def test_yearly_steps_by_calendar_year(self):
		got = [str(period_start("2026-08-01", "Yearly", i)) for i in range(3)]
		self.assertEqual(got, ["2026-08-01", "2027-08-01", "2028-08-01"])

	def test_leap_day_anchor_returns_on_the_next_leap_year(self):
		self.assertEqual(str(period_start("2024-02-29", "Yearly", 1)), "2025-02-28")
		self.assertEqual(str(period_start("2024-02-29", "Yearly", 4)), "2028-02-29")

	def test_periods_are_contiguous(self):
		self.assertEqual(
			period_end("2026-01-31", "Monthly", 0),
			add_days(period_start("2026-01-31", "Monthly", 1), -1),
		)

	def test_period_keys_are_locale_independent(self):
		self.assertEqual(period_key("2026-08-01", "Monthly"), "2026-08")
		self.assertEqual(period_key("2026-08-01", "Yearly"), "2026")

	def test_resume_mid_cycle_skips_to_the_next_one(self):
		self.assertEqual(next_cycle_on_or_after("2026-01-01", "Monthly", "2026-05-10"), 5)

	def test_resume_on_a_billing_day_bills_that_day(self):
		self.assertEqual(next_cycle_on_or_after("2026-01-01", "Monthly", "2026-06-01"), 5)


class TestSubscriptionBilling(IntegrationTestCase):
	START = "2026-08-01"
	END = "2027-03-31"
	PRICE = 10000

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# The engine commits after every subscription so one failure cannot poison the
		# batch. Suppressing commit keeps the whole suite inside the single transaction
		# IntegrationTestCase rolls back at class teardown, leaving the site untouched.
		commit_patch = patch.object(type(frappe.local.db), "commit", lambda self: None)
		commit_patch.start()
		cls.addClassCleanup(commit_patch.stop)

		frappe.flags.mute_emails = True
		cls.company = _pick_company()
		cls.item = _ensure_item("_Test Subscription Service")
		cls.customer = _ensure_customer("_Test Subscription Customer")

	# ------------------------------------------------------------------ helpers

	def make_plan(self, **kwargs):
		values = {
			"doctype": "Customer Subscription Plan",
			"plan_name": f"_Test Plan {frappe.generate_hash(length=8)}",
			"item": self.item,
			"billing_frequency": "Monthly",
			"price": self.PRICE,
			"is_active": 1,
		}
		values.update(kwargs)
		return frappe.get_doc(values).insert()

	def make_subscription(self, plan=None, **kwargs):
		plan = plan or self.make_plan()
		values = {
			"doctype": "Customer Subscription",
			"customer": self.customer,
			"company": self.company,
			"subscription_plan": plan.name,
			"start_date": self.START,
			"end_date": self.END,
			"status": "Active",
		}
		values.update(kwargs)
		return frappe.get_doc(values).insert()

	def invoice_count(self, subscription):
		return frappe.db.count(
			"Sales Invoice", {"customer_subscription": subscription, "docstatus": 1}
		)

	# --------------------------------------------------- the 12 mandatory cases

	def test_01_valid_monthly_subscription_gets_a_schedule(self):
		sub = self.make_subscription()
		self.assertEqual(sub.billing_frequency, "Monthly")
		self.assertEqual(flt(sub.amount), self.PRICE)
		self.assertEqual(getdate(sub.next_billing_date), getdate(self.START))
		self.assertEqual(sub.billing_cycles_completed, 0)

	def test_02_start_date_on_or_after_end_date_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_subscription(start_date="2026-12-01", end_date="2026-11-30")

		# End Date must be strictly after Start Date — a same-day period has no cycle.
		with self.assertRaises(frappe.ValidationError):
			self.make_subscription(start_date="2026-12-01", end_date="2026-12-01")

	def test_03_inactive_plan_blocks_activation(self):
		plan = self.make_plan(is_active=0)
		with self.assertRaises(frappe.ValidationError):
			self.make_subscription(plan=plan, status="Active")

		# A Draft on an inactive plan is fine; activating it later is still refused.
		draft = self.make_subscription(plan=plan, status="Draft")
		draft.status = "Active"
		with self.assertRaises(frappe.ValidationError):
			draft.save()

	def test_04_due_billing_date_creates_a_sales_invoice(self):
		sub = self.make_subscription()
		summary = billing.run_billing(as_of=self.START, subscription=sub.name)
		self.assertEqual(summary["invoiced"], 1)

		invoice = frappe.get_doc("Sales Invoice", summary["invoices"][0])
		self.assertEqual(invoice.docstatus, 1)
		self.assertEqual(invoice.customer, self.customer)
		self.assertEqual(invoice.company, self.company)
		self.assertEqual(getdate(invoice.posting_date), getdate(self.START))
		self.assertEqual(len(invoice.items), 1)
		self.assertEqual(invoice.items[0].item_code, self.item)
		self.assertEqual(flt(invoice.items[0].qty), 1)
		self.assertEqual(flt(invoice.items[0].rate), self.PRICE)
		self.assertEqual(invoice.customer_subscription, sub.name)

		log = frappe.get_doc("Subscription Billing Log", f"{sub.name}-2026-08")
		self.assertEqual(log.status, "Invoiced")
		self.assertEqual(log.sales_invoice, invoice.name)
		self.assertEqual(invoice.subscription_billing_log, log.name)

		sub.reload()
		self.assertEqual(sub.billing_cycles_completed, 1)
		self.assertEqual(getdate(sub.last_billing_date), getdate(self.START))
		self.assertEqual(getdate(sub.next_billing_date), getdate("2026-09-01"))

	def test_05_running_billing_twice_creates_only_one_invoice(self):
		sub = self.make_subscription()
		first = billing.run_billing(as_of=self.START, subscription=sub.name)
		second = billing.run_billing(as_of=self.START, subscription=sub.name)

		self.assertEqual(first["invoiced"], 1)
		self.assertEqual(second["invoiced"], 0)
		self.assertEqual(self.invoice_count(sub.name), 1)
		self.assertEqual(frappe.db.count("Subscription Billing Log", {"subscription": sub.name}), 1)

	def test_06_suspended_subscription_is_not_billed(self):
		sub = self.make_subscription()
		api.suspend_subscription(sub.name)

		summary = billing.run_billing(as_of=self.START, subscription=sub.name)
		self.assertEqual(summary["considered"], 0)
		self.assertEqual(self.invoice_count(sub.name), 0)

	def test_07_cancelled_subscription_stops_future_billing(self):
		sub = self.make_subscription()
		billing.run_billing(as_of=self.START, subscription=sub.name)
		api.cancel_subscription(sub.name, reason="Customer churned")

		sub.reload()
		self.assertEqual(sub.status, "Cancelled")
		self.assertIsNone(sub.next_billing_date)

		later = billing.run_billing(as_of="2026-12-01", subscription=sub.name)
		self.assertEqual(later["invoiced"], 0)
		# The invoice raised before cancellation must survive untouched.
		self.assertEqual(self.invoice_count(sub.name), 1)

	def test_08_subscription_expires_once_end_date_passes(self):
		sub = self.make_subscription(end_date="2026-12-31")
		billing.expire_subscriptions(as_of="2027-01-01")

		sub.reload()
		self.assertEqual(sub.status, "Expired")
		self.assertIsNone(sub.next_billing_date)

	def test_09_zero_or_negative_amount_is_rejected(self):
		for bad_amount in (0, -500):
			with self.assertRaises(frappe.ValidationError):
				self.make_subscription(amount=bad_amount)

	def test_10_cancelling_without_date_or_reason_is_rejected(self):
		sub = self.make_subscription()

		with self.assertRaises(frappe.ValidationError):
			api.cancel_subscription(sub.name, reason="   ")

		sub.reload()
		sub.status = "Cancelled"
		sub.cancellation_date = "2026-08-10"
		with self.assertRaises(frappe.ValidationError):
			sub.save()

		sub.reload()
		sub.status = "Cancelled"
		sub.cancellation_reason = "No date supplied"
		with self.assertRaises(frappe.ValidationError):
			sub.save()

	def test_11_unauthorised_api_access_is_denied(self):
		sub = self.make_subscription()
		user = _ensure_user("_test_subscription_readonly@example.com", ["Read Only User"])

		frappe.set_user(user)
		self.addCleanup(frappe.set_user, "Administrator")

		# Read Only User may read subscriptions but cannot raise invoices...
		with self.assertRaises(frappe.PermissionError):
			api.run_billing_now(subscription=sub.name)
		# ...nor change them.
		with self.assertRaises(frappe.PermissionError):
			api.cancel_subscription(sub.name, reason="Not allowed")

	def test_12_one_failing_subscription_does_not_stop_the_others(self):
		healthy = self.make_subscription()
		broken_plan = self.make_plan()
		broken = self.make_subscription(plan=broken_plan)

		# Point the plan at a missing item behind validation's back, so invoice
		# creation raises for this subscription and only this one.
		frappe.db.set_value(
			"Customer Subscription Plan", broken_plan.name, "item", "___NO_SUCH_ITEM___"
		)

		summary = billing.run_billing(as_of=self.START)

		self.assertIn(broken.name, summary["errors"])
		self.assertGreaterEqual(summary["failed"], 1)

		# The healthy subscription was still invoiced despite the failure.
		self.assertEqual(self.invoice_count(healthy.name), 1)
		self.assertEqual(self.invoice_count(broken.name), 0)

		broken.reload()
		self.assertEqual(broken.billing_cycles_completed, 0)
		self.assertEqual(broken.status, "Active")

		failed_log = frappe.get_doc("Subscription Billing Log", f"{broken.name}-2026-08")
		self.assertEqual(failed_log.status, "Failed")
		self.assertTrue(failed_log.error_message)

	# ------------------------------------------------- supporting guarantees

	def test_replaying_a_billed_cycle_never_duplicates_the_invoice(self):
		"""Simulates the scheduler replaying a cycle whose counter never advanced."""
		sub = self.make_subscription()
		billing.run_billing(as_of=self.START, subscription=sub.name)

		frappe.db.set_value(
			"Customer Subscription",
			sub.name,
			{"billing_cycles_completed": 0, "next_billing_date": self.START, "status": "Active"},
		)

		summary = billing.run_billing(as_of=self.START, subscription=sub.name)
		self.assertEqual(summary["invoiced"], 0)
		self.assertEqual(summary["skipped"], 1)
		self.assertEqual(self.invoice_count(sub.name), 1)

	def test_billing_log_name_is_a_database_level_unique_key(self):
		sub = self.make_subscription()
		billing.run_billing(as_of=self.START, subscription=sub.name)

		with self.assertRaises(frappe.DuplicateEntryError):
			frappe.get_doc(
				{
					"doctype": "Subscription Billing Log",
					"subscription": sub.name,
					"billing_period": "2026-08",
					"period_start_date": self.START,
					"period_end_date": "2026-08-31",
					"billing_date": self.START,
					"amount": self.PRICE,
					"status": "Pending",
				}
			).insert()

	def test_missed_runs_are_caught_up_one_invoice_per_cycle(self):
		sub = self.make_subscription(start_date="2026-05-01")
		summary = billing.run_billing(as_of="2026-08-01", subscription=sub.name)

		# May, June, July and August are all due.
		self.assertEqual(summary["invoiced"], 4)
		self.assertEqual(self.invoice_count(sub.name), 4)

		periods = frappe.get_all(
			"Subscription Billing Log",
			filters={"subscription": sub.name},
			pluck="billing_period",
			order_by="billing_period",
		)
		self.assertEqual(periods, ["2026-05", "2026-06", "2026-07", "2026-08"])

		sub.reload()
		self.assertEqual(getdate(sub.next_billing_date), getdate("2026-09-01"))

	def test_resuming_a_suspension_skips_the_paused_cycles(self):
		sub = self.make_subscription(start_date="2026-05-01")
		billing.run_billing(as_of="2026-05-01", subscription=sub.name)

		api.suspend_subscription(sub.name)
		api.activate_subscription(sub.name)

		sub.reload()
		self.assertEqual(sub.status, "Active")
		# Today is inside the Aug cycle, so billing resumes on 1 Sep — June, July and
		# August are skipped rather than back-billed.
		self.assertEqual(getdate(sub.next_billing_date), getdate("2026-09-01"))
		self.assertEqual(self.invoice_count(sub.name), 1)

	def test_final_cycle_expires_the_subscription(self):
		sub = self.make_subscription(start_date="2026-08-01", end_date="2026-08-20")
		billing.run_billing(as_of="2026-08-01", subscription=sub.name)

		sub.reload()
		self.assertEqual(sub.status, "Expired")
		self.assertIsNone(sub.next_billing_date)
		self.assertEqual(self.invoice_count(sub.name), 1)

	def test_billing_log_with_an_invoice_cannot_be_deleted(self):
		sub = self.make_subscription()
		billing.run_billing(as_of=self.START, subscription=sub.name)

		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Subscription Billing Log", f"{sub.name}-2026-08")

	def test_schedule_fields_are_locked_once_billing_started(self):
		sub = self.make_subscription()
		billing.run_billing(as_of=self.START, subscription=sub.name)

		sub.reload()
		sub.start_date = "2026-07-01"
		with self.assertRaises(frappe.ValidationError):
			sub.save()

	def test_invalid_status_transitions_are_refused(self):
		sub = self.make_subscription()
		api.cancel_subscription(sub.name, reason="Done")

		sub.reload()
		sub.status = "Active"
		with self.assertRaises(frappe.ValidationError):
			sub.save()


# --------------------------------------------------------------------- fixtures


def _pick_company():
	"""First company that can actually raise an invoice — never a hard-coded name."""
	default = frappe.defaults.get_defaults().get("company")
	candidates = ([default] if default else []) + frappe.get_all("Company", pluck="name")
	for name in candidates:
		if name and frappe.db.get_value("Company", name, "default_receivable_account"):
			return name
	raise frappe.ValidationError("No Company with a receivable account is available for testing.")


def _ensure_item(code):
	if frappe.db.exists("Item", code):
		return code
	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
			"stock_uom": "Nos",
			"is_stock_item": 0,
			"is_sales_item": 1,
		}
	)
	# india_compliance makes HSN/SAC mandatory; borrow a real service code if installed.
	if item.meta.has_field("gst_hsn_code"):
		item.gst_hsn_code = frappe.db.get_value("GST HSN Code", {"name": ("like", "99____")}, "name")
	item.insert(ignore_permissions=True)
	return item.name


def _ensure_customer(customer_name):
	existing = frappe.db.exists("Customer", {"customer_name": customer_name})
	if existing:
		return existing
	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": customer_name,
			"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
			"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
		}
	)
	customer.insert(ignore_permissions=True)
	return customer.name


def _ensure_user(email, roles):
	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
	else:
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Subscription Test",
				"send_welcome_email": 0,
			}
		)
		user.insert(ignore_permissions=True)
	user.add_roles(*roles)
	return user.name
