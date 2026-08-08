"""Scheduled entry points."""

import frappe

from subscription_billing.billing import expire_subscriptions, run_billing


def daily_billing():
	"""Daily hook: invoice everything due, then expire subscriptions past their End Date."""
	summary = run_billing()
	summary["expired"] = expire_subscriptions()
	frappe.logger("subscription_billing").info(summary)
	return summary
