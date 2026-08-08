app_name = "subscription_billing"
app_title = "Subscription Billing"
app_publisher = "Entries"
app_description = "Subscription Management and Recurring Billing for ERPNext"
app_email = "ashish@entries.ai"
app_license = "mit"

# Roles and the Sales Invoice back-reference fields. Idempotent, so migrate re-runs safely.
after_install = "subscription_billing.install.after_install"
after_migrate = "subscription_billing.install.after_migrate"

# Invoice generation is I/O heavy, so it belongs on the long queue rather than `daily`.
scheduler_events = {
	"daily_long": ["subscription_billing.tasks.daily_billing"],
}
