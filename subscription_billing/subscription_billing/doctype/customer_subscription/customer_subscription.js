// Copyright (c) 2026, Entries and contributors
// For license information, please see license.txt

// Client-side rules here are for fast feedback only. Every one of them is also
// enforced in customer_subscription.py, which is what actually protects the data.

const API = "subscription_billing.api";
const TERMINAL = ["Cancelled", "Expired"];

frappe.ui.form.on("Customer Subscription", {
	setup(frm) {
		frm.set_query("subscription_plan", () => ({ filters: { is_active: 1 } }));
	},

	refresh(frm) {
		add_lifecycle_buttons(frm);
		show_schedule_hint(frm);
	},

	subscription_plan(frm) {
		if (!frm.doc.subscription_plan) return;
		frappe.db
			.get_value("Customer Subscription Plan", frm.doc.subscription_plan, [
				"billing_frequency",
				"price",
			])
			.then(({ message }) => {
				if (!message) return;
				frm.set_value("billing_frequency", message.billing_frequency);
				if (!frm.doc.amount) frm.set_value("amount", message.price);
			});
	},

	start_date: (frm) => reject_invalid_period(frm, "start_date"),
	end_date: (frm) => reject_invalid_period(frm, "end_date"),
});

// End Date must be strictly after Start Date. Checked the moment a date is picked
// rather than on save, and the message names whichever date the user just changed.
// customer_subscription.py enforces the same rule server-side.
function reject_invalid_period(frm, edited_field) {
	if (!frm.doc.start_date || !frm.doc.end_date) return;

	const days = frappe.datetime.get_diff(frm.doc.end_date, frm.doc.start_date);
	if (days > 0) return;

	let message;
	if (days === 0) {
		message = __("Start Date and End Date cannot be the same.");
	} else if (edited_field === "start_date") {
		message = __("Start Date cannot be after End Date.");
	} else {
		message = __("End Date cannot be before Start Date.");
	}

	// Clear the date just entered, so the field the user did not touch is preserved.
	frm.set_value(edited_field, null);
	frappe.msgprint({ title: __("Invalid Period"), message: message, indicator: "red" });
}

function show_schedule_hint(frm) {
	frm.dashboard.clear_headline();
	if (frm.is_new()) return;

	if (frm.doc.status === "Suspended") {
		frm.dashboard.set_headline(
			__("Suspended — no invoices are generated. Cycles missed while suspended are skipped on resume."),
			"orange"
		);
	} else if (frm.doc.next_billing_date) {
		frm.dashboard.set_headline(
			__("Next invoice on {0} for {1}", [
				frappe.datetime.str_to_user(frm.doc.next_billing_date),
				format_currency(frm.doc.amount),
			])
		);
	} else if (!TERMINAL.includes(frm.doc.status)) {
		frm.dashboard.set_headline(__("No further billing cycles fit before the End Date."), "orange");
	}
}

function add_lifecycle_buttons(frm) {
	if (frm.is_new() || TERMINAL.includes(frm.doc.status)) return;
	const group = __("Subscription");

	if (["Draft", "Suspended"].includes(frm.doc.status)) {
		frm.add_custom_button(__("Activate"), () => call(frm, "activate_subscription"), group);
	}
	if (frm.doc.status === "Active") {
		frm.add_custom_button(__("Suspend"), () => call(frm, "suspend_subscription"), group);
		frm.add_custom_button(__("Run Billing Now"), () => run_billing(frm), group);
	}
	frm.add_custom_button(__("Cancel Subscription"), () => prompt_cancel(frm), group);
	frm.add_custom_button(__("Billing Preview"), () => show_preview(frm), group);
}

function call(frm, method, args = {}) {
	frappe.call({
		method: `${API}.${method}`,
		args: { subscription: frm.doc.name, ...args },
		freeze: true,
		callback: () => frm.reload_doc(),
	});
}

function prompt_cancel(frm) {
	frappe.prompt(
		[
			{
				fieldname: "cancellation_date",
				label: __("Cancellation Date"),
				fieldtype: "Date",
				reqd: 1,
				default: frappe.datetime.get_today(),
			},
			{
				fieldname: "reason",
				label: __("Cancellation Reason"),
				fieldtype: "Small Text",
				reqd: 1,
			},
		],
		(values) => call(frm, "cancel_subscription", values),
		__("Cancel Subscription"),
		__("Confirm")
	);
}

function run_billing(frm) {
	frappe.call({
		method: `${API}.run_billing_now`,
		args: { subscription: frm.doc.name },
		freeze: true,
		freeze_message: __("Generating invoices..."),
		callback: ({ message }) => {
			frappe.msgprint({
				title: __("Billing Run Complete"),
				indicator: message.failed ? "red" : "green",
				message: __("Invoiced: {0} &middot; Skipped as already billed: {1} &middot; Failed: {2}", [
					message.invoiced,
					message.skipped,
					message.failed,
				]),
			});
			frm.reload_doc();
		},
	});
}

function show_preview(frm) {
	frappe.call({
		method: `${API}.get_billing_preview`,
		args: { subscription: frm.doc.name, cycles: 12 },
		callback: ({ message }) => {
			if (!message || !message.length) {
				frappe.msgprint(__("No upcoming billing cycles."));
				return;
			}
			const rows = message
				.map(
					(r) =>
						`<tr><td>${r.cycle}</td><td>${frappe.datetime.str_to_user(r.billing_date)}</td>
						 <td>${frappe.datetime.str_to_user(r.period_end_date)}</td>
						 <td class="text-right">${format_currency(r.amount)}</td></tr>`
				)
				.join("");
			frappe.msgprint({
				title: __("Upcoming Billing Cycles"),
				message: `<table class="table table-bordered"><thead><tr>
					<th>${__("Cycle")}</th><th>${__("Billing Date")}</th>
					<th>${__("Period End")}</th><th class="text-right">${__("Amount")}</th>
					</tr></thead><tbody>${rows}</tbody></table>`,
			});
		},
	});
}
