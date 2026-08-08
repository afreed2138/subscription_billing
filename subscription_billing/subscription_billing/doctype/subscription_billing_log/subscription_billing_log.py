# Copyright (c) 2026, Entries and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class SubscriptionBillingLog(Document):
	def validate(self):
		if self.status == "Invoiced" and not self.sales_invoice:
			frappe.throw(_("A log marked Invoiced must reference a Sales Invoice."))

	def on_trash(self):
		"""This log is the duplicate-billing guard — deleting it would re-open a billed cycle."""
		if self.sales_invoice:
			frappe.throw(
				_("Cannot delete this log because Sales Invoice {0} was raised for it. Cancel the invoice instead.").format(
					frappe.bold(self.sales_invoice)
				),
				title=_("Billing History Protected"),
			)
