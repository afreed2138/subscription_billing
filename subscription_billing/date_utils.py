"""Anchor-based period arithmetic for recurring billing.

Every period is derived from the subscription's start date plus a cycle offset,
never by advancing the previous period. That keeps month-end billing days stable
(31 Jan -> 28 Feb -> 31 Mar) instead of drifting downwards.
"""

import frappe
from frappe import _
from frappe.utils import add_days, add_months, getdate

MONTHS_PER_CYCLE = {"Monthly": 1, "Yearly": 12}


def cycle_step(frequency):
	"""Number of months one billing cycle advances."""
	step = MONTHS_PER_CYCLE.get(frequency)
	if not step:
		frappe.throw(_("Unsupported Billing Frequency: {0}").format(frequency))
	return step


def period_start(anchor, frequency, cycle):
	"""Start date of the 0-based `cycle`, measured from `anchor`."""
	return getdate(add_months(getdate(anchor), cycle * cycle_step(frequency)))


def period_end(anchor, frequency, cycle):
	"""Last day of `cycle` — the day before the next cycle begins."""
	return add_days(period_start(anchor, frequency, cycle + 1), -1)


def period_key(start, frequency):
	"""Locale-independent period label: '2026-08' monthly, '2026' yearly."""
	start = getdate(start)
	return start.strftime("%Y") if frequency == "Yearly" else start.strftime("%Y-%m")


def cycles_elapsed(anchor, frequency, upto):
	"""How many whole cycles have started on or before `upto`."""
	anchor, upto = getdate(anchor), getdate(upto)
	if upto < anchor:
		return 0
	step = cycle_step(frequency)
	approx = ((upto.year - anchor.year) * 12 + (upto.month - anchor.month)) // step
	# relativedelta clamping can put the approximation one cycle either side.
	while period_start(anchor, frequency, approx + 1) <= upto:
		approx += 1
	while approx > 0 and period_start(anchor, frequency, approx) > upto:
		approx -= 1
	return approx


def next_cycle_on_or_after(anchor, frequency, on_date):
	"""Index of the first cycle whose period starts on or after `on_date`."""
	anchor, on_date = getdate(anchor), getdate(on_date)
	if on_date <= anchor:
		return 0
	current = cycles_elapsed(anchor, frequency, on_date)
	return current if period_start(anchor, frequency, current) == on_date else current + 1
