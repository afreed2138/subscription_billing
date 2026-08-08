"""Post-install setup: roles and the Sales Invoice back-references.

Both steps are idempotent so they can safely re-run on every migrate.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

ROLES = ("Subscription Manager", "Billing User", "Read Only User")

# ERPNext's Sales Invoice already has a `subscription` field pointing at its own
# Subscription doctype, so ours are named distinctly and anchored after it.
CUSTOM_FIELDS = {
	"Sales Invoice": [
		{
			"fieldname": "customer_subscription",
			"label": "Customer Subscription",
			"fieldtype": "Link",
			"options": "Customer Subscription",
			"insert_after": "subscription",
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "subscription_billing_log",
			"label": "Subscription Billing Log",
			"fieldtype": "Link",
			"options": "Subscription Billing Log",
			"insert_after": "customer_subscription",
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
		},
	]
}


def after_install():
	create_roles()
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)


def after_migrate():
	after_install()


def create_roles():
	for role in ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc(
				{"doctype": "Role", "role_name": role, "desk_access": 1}
			).insert(ignore_permissions=True)
