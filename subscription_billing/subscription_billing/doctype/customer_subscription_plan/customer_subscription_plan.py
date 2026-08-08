# Copyright (c) 2026, Entries and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class CustomerSubscriptionPlan(Document):
	def validate(self):
		self.validate_price()
		self.validate_item()

	def validate_price(self):
		if flt(self.price) <= 0:
			frappe.throw(_("Price must be greater than zero."), title=_("Invalid Price"))

	def validate_item(self):
		"""Fail here rather than deep inside Sales Invoice validation months later."""
		disabled, is_sales_item = frappe.db.get_value("Item", self.item, ["disabled", "is_sales_item"])
		if disabled:
			frappe.throw(_("Item {0} is disabled.").format(frappe.bold(self.item)))
		if not is_sales_item:
			frappe.throw(
				_("Item {0} is not a Sales Item, so it cannot be invoiced.").format(frappe.bold(self.item))
			)

	def on_update(self):
		"""Deactivating a plan blocks new activations; running subscriptions continue."""
		if self.has_value_changed("is_active") and not self.is_active:
			count = frappe.db.count(
				"Customer Subscription", {"subscription_plan": self.name, "status": "Active"}
			)
			if count:
				frappe.msgprint(
					_("{0} Active subscription(s) still use this plan and will keep billing.").format(count),
					indicator="orange",
					title=_("Plan Deactivated"),
				)
