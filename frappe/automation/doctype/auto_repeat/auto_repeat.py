# Copyright (c) 2018, Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
"""Auto Repeat — recurring document creation, with two modes.

Originally (pre-WP GA-0001-05+06): Auto Repeat had a single behaviour — on every
schedule tick it deep-copied the source document via `frappe.copy_doc` and
inserted the copy. The copy carried whatever was on the source verbatim.

That behaviour is preserved as **Copy mode** (the default; existing Auto Repeats
keep behaving byte-identically when all refresh switches are off). The WP added:

1. **Source-validation handling** (GAP-001, GAP-009)
   `skip_if_source_cancelled` + `follow_amendment_chain` resolve the source to
   the latest non-cancelled version — see `get_authoritative_source` /
   `find_latest_amendment`. When the source is cancelled with no valid amendment,
   the AR self-disables, logs, and (optionally) emails recipients.

2. **Copy-mode dynamic refresh** (GAP-002..008, GAP-019..020, GAP-024..027)
   `refresh_mode` + per-field switches re-derive prices, FX rate, taxes, payment
   schedule, sales/purchase/item tax templates, shipping rule, and cost-center
   allocation for the new posting date. Each helper is bounded, idempotent, and
   silently no-ops when the optional ERPNext dependency is missing.

3. **Reversal mode** (GAP-012..015, GAP-021..023)
   `repeat_type = "Reversal"` (Journal Entry only) hands off to ERPNext's
   `make_reverse_journal_entry`, schedules for "first of next month" or a
   specific date via the existing scheduler dispatch path, and disables itself
   after a single execution. `reversal_exchange_rate_type`,
   `reversal_tax_mode`, and `reversal_cost_center_mode` toggle Original-Rate
   (true reversal, perfect offset) vs Current-Rate / Recalculate / Apply-Current
   semantics for adjustment scenarios. Non-true-reversal choices are warned at
   save time but never blocked.

GAP-010 (`amended_from` cleared by `copy_doc`) is architecturally resolved:
`get_authoritative_source` walks the amendment chain to the latest version
*before* `copy_doc` runs, so the cleared `amended_from` on the new doc is the
correct outcome — a recurring copy is not an amendment of the source.

GAP-011 (immutable-ledger awareness) is surfaced as warnings on Reversal
configurations that break true-reversal semantics (Current Rate, Recalculate
Tax, Apply Current Allocation). The warnings are non-blocking — those choices
are legitimate for adjustment / restatement scenarios.

Full per-gap rationale: see `badia_docs/signed_off_wp/imp_ga-0001-05+06.md` in
the originating implementation tree, or the upstream PR description.
"""

from datetime import timedelta

from dateutil.relativedelta import relativedelta

import frappe
from frappe import _
from frappe.automation.doctype.assignment_rule.assignment_rule import get_repeated
from frappe.contacts.doctype.contact.contact import (
	get_contacts_linked_from,
	get_contacts_linking_to,
)
from frappe.core.doctype.communication.email import make
from frappe.desk.form.assign_to import add as assign_to
from frappe.model.document import Document
from frappe.utils import (
	add_days,
	add_months,
	cstr,
	flt,
	get_first_day,
	get_last_day,
	getdate,
	month_diff,
	split_emails,
	today,
)
from frappe.utils.background_jobs import get_jobs
from frappe.utils.jinja import validate_template
from frappe.utils.user import get_system_managers

month_map = {"Monthly": 1, "Quarterly": 3, "Half-yearly": 6, "Yearly": 12}
week_map = {
	"Monday": 0,
	"Tuesday": 1,
	"Wednesday": 2,
	"Thursday": 3,
	"Friday": 4,
	"Saturday": 5,
	"Sunday": 6,
}


class AutoRepeat(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.automation.doctype.auto_repeat_day.auto_repeat_day import AutoRepeatDay
		from frappe.automation.doctype.auto_repeat_user.auto_repeat_user import AutoRepeatUser
		from frappe.types import DF

		apply_pricing_rules: DF.Check
		assignee: DF.TableMultiSelect[AutoRepeatUser]
		auto_submit_reversal: DF.Check
		current_source_document: DF.DynamicLink | None
		disabled: DF.Check
		end_date: DF.Date | None
		follow_amendment_chain: DF.Check
		frequency: DF.Literal[
			"", "Daily", "Weekly", "Fortnightly", "Monthly", "Quarterly", "Half-yearly", "Yearly"
		]
		generate_separate_documents_for_each_assignee: DF.Check
		message: DF.Text | None
		next_schedule_date: DF.Date | None
		notify_by_email: DF.Check
		print_format: DF.Link | None
		recalculate_payment_terms: DF.Check
		recalculate_taxes: DF.Check
		recipients: DF.SmallText | None
		reference_doctype: DF.Link
		reference_document: DF.DynamicLink
		refresh_exchange_rate: DF.Check
		refresh_item_tax_template: DF.Check
		refresh_mode: DF.Literal["Copy Original", "Recalculate"]
		refresh_prices: DF.Check
		refresh_purchase_tax_template: DF.Check
		refresh_sales_tax_template: DF.Check
		refresh_shipping_rule: DF.Check
		repeat_on_day: DF.Int
		repeat_on_days: DF.Table[AutoRepeatDay]
		repeat_on_last_day: DF.Check
		repeat_type: DF.Literal["Copy", "Reversal"]
		respect_cost_center_allocation: DF.Check
		reversal_cost_center_mode: DF.Literal["Use Original", "Apply Current Allocation"]
		reversal_exchange_rate_type: DF.Literal["Original Rate", "Current Rate"]
		reversal_tax_mode: DF.Literal["Use Original", "Recalculate for Posting Date"]
		reverse_date: DF.Date | None
		reverse_on_next_month: DF.Check
		skip_if_source_cancelled: DF.Check
		start_date: DF.Date
		status: DF.Literal["", "Active", "Disabled", "Completed"]
		subject: DF.Data | None
		submit_on_creation: DF.Check
		template: DF.Link | None
	# end: auto-generated types

	def validate(self):
		self.update_status()
		self.validate_repeat_type()
		self.validate_reference_doctype()
		self.validate_submit_on_creation()
		self.validate_dates()
		self.validate_email_id()
		self.validate_auto_repeat_days()
		self.set_dates()
		self.update_auto_repeat_id()
		self.unlink_if_applicable()

		validate_template(self.subject or "")
		validate_template(self.message or "")

	def before_save(self):
		self.warn_on_non_true_reversal()

	def before_insert(self):
		if not frappe.in_test:
			today_date = getdate()
			if getdate(self.start_date) < today_date:
				self.start_date = today_date

	def on_update(self):
		frappe.get_doc(self.reference_doctype, self.reference_document).notify_update()

	def on_trash(self):
		frappe.db.set_value(self.reference_doctype, self.reference_document, "auto_repeat", "")
		frappe.get_doc(self.reference_doctype, self.reference_document).notify_update()

	def set_dates(self):
		if self.disabled:
			self.next_schedule_date = None
			return

		if self.repeat_type == "Reversal":
			# Reversal mode is single-execution; next_schedule_date is the configured reversal date
			# so the existing scheduler dispatch path picks it up on the right day.
			if self.reverse_on_next_month:
				self.next_schedule_date = get_first_day(add_months(getdate(), 1))
			elif self.reverse_date:
				self.next_schedule_date = getdate(self.reverse_date)
			return

		self.next_schedule_date = self.get_next_schedule_date(schedule_date=self.start_date)
		if self.end_date and getdate(self.end_date) < getdate(self.next_schedule_date):
			frappe.throw(_("The Next Scheduled Date cannot be later than the End Date."))

	def unlink_if_applicable(self):
		if self.status == "Completed" or self.disabled:
			frappe.db.set_value(self.reference_doctype, self.reference_document, "auto_repeat", "")

	def validate_reference_doctype(self):
		if frappe.in_test or frappe.flags.in_patch:
			return
		if not frappe.get_meta(self.reference_doctype).allow_auto_repeat:
			frappe.throw(
				_("Enable Allow Auto Repeat for the doctype {0} in Customize Form").format(
					self.reference_doctype
				)
			)

	def validate_submit_on_creation(self):
		if self.submit_on_creation and not frappe.get_meta(self.reference_doctype).is_submittable:
			frappe.throw(
				_("Cannot enable {0} for a non-submittable doctype").format(
					frappe.bold(_("Submit on Creation"))
				)
			)

	def validate_dates(self):
		if frappe.flags.in_patch:
			return

		if self.end_date:
			end_date = getdate(self.end_date)

			self.validate_from_to_dates("start_date", "end_date")

			if end_date == getdate():
				frappe.throw(_("End Date cannot be today."))
			if end_date == getdate(self.start_date):
				frappe.throw(
					_("{0} should not be same as {1}").format(
						frappe.bold(_("End Date")), frappe.bold(_("Start Date"))
					)
				)

	def validate_email_id(self):
		if self.notify_by_email:
			if self.recipients:
				email_list = split_emails(self.recipients.replace("\n", ""))
				from frappe.utils import validate_email_address

				for email in email_list:
					if not validate_email_address(email):
						frappe.throw(_("{0} is an invalid email address in 'Recipients'").format(email))
			else:
				frappe.throw(_("'Recipients' not specified"))

	def validate_auto_repeat_days(self):
		auto_repeat_days = self.get_auto_repeat_days()
		if len(set(auto_repeat_days)) != len(auto_repeat_days):
			repeated_days = get_repeated(auto_repeat_days)
			plural = "s" if len(repeated_days) > 1 else ""

			frappe.throw(
				_("Auto Repeat Day{0} {1} has been repeated.").format(
					plural, frappe.bold(", ".join(repeated_days))
				)
			)

	def update_auto_repeat_id(self):
		# check if document is already on auto repeat
		auto_repeat = frappe.db.get_value(self.reference_doctype, self.reference_document, "auto_repeat")
		if auto_repeat and auto_repeat != self.name and not frappe.flags.in_patch:
			frappe.throw(
				_("The {0} is already on auto repeat {1}").format(self.reference_document, auto_repeat)
			)
		else:
			frappe.db.set_value(self.reference_doctype, self.reference_document, "auto_repeat", self.name)

	def update_status(self):
		# A Reversal-mode AR that has finished its single execution sets disabled=1
		# and status="Completed" directly via db_set; preserve that on the next save.
		if self.repeat_type == "Reversal" and self.disabled and self.status == "Completed":
			return
		if self.disabled:
			self.status = "Disabled"
		elif self.is_completed():
			self.status = "Completed"
		else:
			self.status = "Active"

	def is_completed(self):
		return self.end_date and getdate(self.end_date) < getdate(today())

	@frappe.whitelist()
	def get_auto_repeat_schedule(self):
		schedule_details = []
		start_date = getdate(self.start_date)
		end_date = getdate(self.end_date)

		if not self.end_date:
			next_date = self.get_next_schedule_date(schedule_date=start_date)
			row = {
				"reference_document": self.reference_document,
				"frequency": self.frequency,
				"next_scheduled_date": next_date,
			}
			schedule_details.append(row)

		if self.end_date:
			next_date = self.get_next_schedule_date(schedule_date=start_date, for_full_schedule=True)

			while getdate(next_date) <= getdate(end_date):
				row = {
					"reference_document": self.reference_document,
					"frequency": self.frequency,
					"next_scheduled_date": next_date,
				}
				schedule_details.append(row)
				next_date = self.get_next_schedule_date(schedule_date=next_date, for_full_schedule=True)

		return schedule_details

	def create_documents(self):
		try:
			if self.generate_separate_documents_for_each_assignee and self.assignee:
				new_docs = self.make_new_documents()
			else:
				new_docs = self.make_new_document([assignee.user for assignee in self.assignee])
			if self.notify_by_email and self.recipients:
				if isinstance(new_docs, list):
					for new_doc in new_docs:
						self.send_notification(new_doc)
				else:
					self.send_notification(new_docs)
		except Exception:
			error_log = self.log_error(
				_("Auto repeat failed. Please enable auto repeat after fixing the issues.")
			)

			self.disable_auto_repeat()

			if self.reference_document and not frappe.in_test:
				self.notify_error_to_user(error_log)

	def make_new_documents(self):
		docs = []
		for assignee in self.assignee:
			new_doc = self.make_new_document(assignee=[assignee.user])
			docs.append(new_doc)
		return docs

	def make_new_document(self, assignee=None):
		reference_doc = self.get_authoritative_source()
		if reference_doc is None:
			return self.handle_no_valid_source()

		if self.repeat_type == "Reversal":
			return self.make_reversal_document(reference_doc)
		return self.make_copy_document(reference_doc, assignee)

	def make_copy_document(self, reference_doc, assignee=None):
		new_doc = frappe.copy_doc(reference_doc, ignore_no_copy=False)
		self.update_doc(new_doc, reference_doc)
		new_doc.flags.updater_reference = {
			"doctype": self.doctype,
			"docname": self.name,
			"label": _("via Auto Repeat"),
		}

		# Apply per-field refresh switches before insert
		if self.refresh_prices:
			self.refresh_item_prices(new_doc)
		if self.refresh_exchange_rate:
			self.refresh_conversion_rate(new_doc)
		if self.refresh_sales_tax_template:
			self.refresh_sales_tax_template_for(new_doc)
		if self.refresh_purchase_tax_template:
			self.refresh_purchase_tax_template_for(new_doc)
		if self.refresh_item_tax_template:
			self.refresh_item_tax_template_for(new_doc)
		if self.refresh_shipping_rule:
			self.refresh_shipping_rule_for(new_doc)
		if self.recalculate_taxes:
			self.recalculate_document_taxes(new_doc)
		if self.recalculate_payment_terms:
			self.recalculate_payment_schedule(new_doc)
		if self.respect_cost_center_allocation:
			self.apply_cost_center_allocation(new_doc)

		new_doc.insert(ignore_permissions=True)
		if assignee:
			args = {
				"assign_to": assignee,
				"doctype": self.reference_doctype,
				"name": new_doc.name,
				"description": new_doc.get_title(),
			}
			assign_to(args=args)
		if self.submit_on_creation:
			new_doc.submit()

		return new_doc

	def update_doc(self, new_doc, reference_doc):
		new_doc.docstatus = 0
		if new_doc.meta.get_field("set_posting_time"):
			new_doc.set("set_posting_time", 1)

		if new_doc.meta.get_field("auto_repeat"):
			new_doc.set("auto_repeat", self.name)

		for fieldname in [
			"naming_series",
			"ignore_pricing_rule",
			"posting_time",
			"select_print_heading",
			"user_remark",
			"remarks",
			"owner",
		]:
			if new_doc.meta.get_field(fieldname):
				new_doc.set(fieldname, reference_doc.get(fieldname))

		for data in new_doc.meta.fields:
			if data.fieldtype == "Date" and data.reqd:
				new_doc.set(data.fieldname, self.next_schedule_date)

		self.set_auto_repeat_period(new_doc)

		auto_repeat_doc = frappe.get_doc("Auto Repeat", self.name)

		# for any action that needs to take place after the recurring document creation
		# on recurring method of that doctype is triggered
		new_doc.run_method("on_recurring", reference_doc=reference_doc, auto_repeat_doc=auto_repeat_doc)

	def set_auto_repeat_period(self, new_doc):
		mcount = month_map.get(self.frequency)
		if mcount and new_doc.meta.get_field("from_date") and new_doc.meta.get_field("to_date"):
			last_ref_doc = frappe.get_all(
				doctype=self.reference_doctype,
				fields=["name", "from_date", "to_date"],
				filters=[
					["auto_repeat", "=", self.name],
					["docstatus", "<", 2],
				],
				order_by="creation desc",
				limit=1,
			)

			if not last_ref_doc:
				return

			from_date = get_next_date(last_ref_doc[0].from_date, mcount)

			if (cstr(get_first_day(last_ref_doc[0].from_date)) == cstr(last_ref_doc[0].from_date)) and (
				cstr(get_last_day(last_ref_doc[0].to_date)) == cstr(last_ref_doc[0].to_date)
			):
				to_date = get_last_day(get_next_date(last_ref_doc[0].to_date, mcount))
			else:
				to_date = get_next_date(last_ref_doc[0].to_date, mcount)

			new_doc.set("from_date", from_date)
			new_doc.set("to_date", to_date)

	def get_next_schedule_date(self, schedule_date, for_full_schedule=False):
		"""
		Return the next schedule date for auto repeat after a recurring document has been created.
		Add required offset to the schedule_date param and return the next schedule date.

		:param schedule_date: The date when the last recurring document was created.
		:param for_full_schedule: If True, return the immediate next schedule date, else the full schedule.
		"""
		if month_map.get(self.frequency):
			month_count = month_map.get(self.frequency) + month_diff(schedule_date, self.start_date) - 1
		else:
			month_count = 0

		day_count = 0
		if month_count:
			day_count = 31 if self.repeat_on_last_day else self.repeat_on_day or None
			next_date = get_next_date(self.start_date, month_count, day_count)
		else:
			days = self.get_days(schedule_date)
			next_date = add_days(schedule_date, days)

		# next schedule date should be after or on current date
		if not for_full_schedule:
			while getdate(next_date) < getdate(today()):
				if month_count:
					month_count += month_map.get(self.frequency, 0)
					next_date = get_next_date(self.start_date, month_count, day_count)
				else:
					days = self.get_days(next_date)
					next_date = add_days(next_date, days)

			if self.end_date and getdate(next_date) > getdate(self.end_date):
				next_date = schedule_date

		return next_date

	def get_days(self, schedule_date):
		if self.frequency == "Weekly":
			days = self.get_offset_for_weekly_frequency(schedule_date)
		elif self.frequency == "Fortnightly":
			days = 14
		else:
			# daily frequency
			days = 1

		return days

	def get_offset_for_weekly_frequency(self, schedule_date):
		# if weekdays are not set, offset is 7 from current schedule date
		if not self.repeat_on_days:
			return 7

		repeat_on_days = self.get_auto_repeat_days()
		current_schedule_day = getdate(schedule_date).weekday()
		weekdays = list(week_map.keys())

		# if repeats on more than 1 day or
		# start date's weekday is not in repeat days, then get next weekday
		# else offset is 7
		if len(repeat_on_days) > 1 or weekdays[current_schedule_day] not in repeat_on_days:
			weekday = get_next_weekday(current_schedule_day, repeat_on_days)
			next_weekday_number = week_map.get(weekday, 0)
			# offset for upcoming weekday
			return timedelta((7 + next_weekday_number - current_schedule_day) % 7).days
		return 7

	def get_auto_repeat_days(self):
		return [d.day for d in self.get("repeat_on_days", [])]

	def send_notification(self, new_doc):
		"""Notify concerned people about recurring document generation"""
		subject = self.subject or ""
		message = self.message or ""

		if not self.subject:
			subject = _("New {0}: {1}").format(new_doc.doctype, new_doc.name)
		elif "{" in self.subject:
			subject = frappe.render_template(self.subject, {"doc": new_doc})

		print_format = self.print_format or "Standard"
		error_string = None

		try:
			attachments = [
				frappe.attach_print(
					new_doc.doctype, new_doc.name, file_name=new_doc.name, print_format=print_format
				)
			]

		except frappe.PermissionError:
			error_string = _("A recurring {0} {1} has been created for you via Auto Repeat {2}.").format(
				new_doc.doctype, new_doc.name, self.name
			)
			error_string += "<br><br>"

			error_string += _(
				"{0}: Failed to attach new recurring document. To enable attaching document in the auto repeat notification email, enable {1} in Print Settings"
			).format(frappe.bold(_("Note")), frappe.bold(_("Allow Print for Draft")))
			attachments = None

		if error_string:
			message = error_string
		elif not self.message:
			message = _("Please find attached {0}: {1}").format(new_doc.doctype, new_doc.name)
		elif "{" in self.message:
			message = frappe.render_template(self.message, {"doc": new_doc})

		make(
			doctype=new_doc.doctype,
			name=new_doc.name,
			recipients=self.recipients,
			subject=subject,
			content=message,
			attachments=attachments,
			send_email=1,
		)

	@frappe.whitelist()
	def fetch_linked_contacts(self):
		if self.reference_doctype and self.reference_document:
			res = get_contacts_linking_to(
				self.reference_doctype, self.reference_document, fields=["email_id"]
			)
			res += get_contacts_linked_from(
				self.reference_doctype, self.reference_document, fields=["email_id"]
			)
			email_ids = {d.email_id for d in res}
			if not email_ids:
				frappe.msgprint(_("No contacts linked to document"), alert=True)
			else:
				self.recipients = ", ".join(email_ids)

	def disable_auto_repeat(self):
		frappe.db.set_value("Auto Repeat", self.name, "disabled", 1)

	def notify_error_to_user(self, error_log):
		recipients = list(get_system_managers(only_name=True))
		recipients.append(self.owner)
		subject = _("Auto Repeat Document Creation Failed")

		form_link = frappe.utils.get_link_to_form(self.reference_doctype, self.reference_document)
		auto_repeat_failed_for = _("Auto Repeat failed for {0}").format(form_link)

		error_log_link = frappe.utils.get_link_to_form("Error Log", error_log.name)
		error_log_message = _("Check the Error Log for more information: {0}").format(error_log_link)

		frappe.sendmail(
			recipients=recipients,
			subject=subject,
			template="auto_repeat_fail",
			args={"auto_repeat_failed_for": auto_repeat_failed_for, "error_log_message": error_log_message},
			header=[subject, "red"],
		)

	# ──────────────────────────────────────────────────────────────────────
	# WP GA-0001-05+06 — Repeat Type validation, source resolution, refresh,
	# and reversal helpers.
	# ──────────────────────────────────────────────────────────────────────

	def validate_repeat_type(self):
		if self.repeat_type != "Reversal":
			return

		if self.reference_doctype != "Journal Entry":
			frappe.throw(_("Reversal mode is only supported for Journal Entry"))

		if not frappe.db.exists("DocType", "Journal Entry"):
			# Frappe-only site without ERPNext — Reversal cannot work
			frappe.throw(_("Reversal mode requires the ERPNext app (Journal Entry doctype not found)"))

		if not (self.reverse_on_next_month or self.reverse_date):
			frappe.throw(
				_(
					"Reversal mode requires either 'Reverse on First Day of Next Month' "
					"or a specific 'Reversal Date'"
				)
			)

	def warn_on_non_true_reversal(self):
		"""Surface a warning when Reversal-mode settings break true-reversal semantics.

		These choices are legitimate for adjustment scenarios (revaluation, restatement),
		but break the perfect-offset property required for immutable-ledger compliance.
		Warn — do not throw.
		"""
		if self.repeat_type != "Reversal":
			return
		warnings = []
		if self.reversal_exchange_rate_type == "Current Rate":
			warnings.append(
				_(
					"Using 'Current Rate' for the reversal will create an FX gain/loss "
					"instead of a perfect offset of the original entry."
				)
			)
		if self.reversal_tax_mode == "Recalculate for Posting Date":
			warnings.append(
				_(
					"Recalculating taxes on the reversal posting date breaks immutable-ledger "
					"compliance for true reversals — only enable for adjustment scenarios."
				)
			)
		if self.reversal_cost_center_mode == "Apply Current Allocation":
			warnings.append(
				_(
					"Applying current Cost Center Allocation rules to the reversal breaks "
					"immutable-ledger compliance for true reversals."
				)
			)
		if warnings:
			frappe.msgprint(
				"<br>".join(warnings),
				title=_("Reversal Configuration Warning"),
				indicator="orange",
			)

	def get_authoritative_source(self):
		"""Resolve the source document, following the amendment chain when configured.

		Returns the live source doc, or None when the source is cancelled and either
		(a) follow_amendment_chain=0, or (b) no non-cancelled amendment exists.
		"""
		reference_doc = frappe.get_doc(self.reference_doctype, self.reference_document)

		if hasattr(reference_doc, "docstatus") and reference_doc.docstatus == 2:
			if self.follow_amendment_chain:
				latest = self.find_latest_amendment(reference_doc)
				if latest:
					self.db_set("current_source_document", latest.name)
					return latest
			return None

		self.db_set("current_source_document", reference_doc.name)
		return reference_doc

	def find_latest_amendment(self, cancelled_doc):
		"""Walk the amended_from chain forward and return the latest non-cancelled successor.

		Multi-hop: if cancelled_doc was amended into a successor that is ALSO cancelled,
		recurse on that successor to find the next-generation amendment. Returns None
		only when the chain ends at a cancelled doc with no further amendments.
		"""
		amended = frappe.db.get_value(
			self.reference_doctype,
			{"amended_from": cancelled_doc.name},
			["name", "docstatus"],
			as_dict=True,
		)
		if not amended:
			return None
		if amended.docstatus == 2:
			# Successor is also cancelled — walk further forward.
			return self.find_latest_amendment(
				frappe.get_doc(self.reference_doctype, amended.name)
			)
		return frappe.get_doc(self.reference_doctype, amended.name)

	def handle_no_valid_source(self):
		"""Source is cancelled with no valid amendment — skip-and-disable, or throw."""
		msg = (
			f"Auto Repeat {self.name}: source {self.reference_document} is cancelled "
			f"and no valid amendment was found."
		)
		if self.skip_if_source_cancelled:
			frappe.log_error(title="Auto Repeat Skipped", message=msg)
			self.db_set("disabled", 1)
			self.db_set("status", "Disabled")
			if self.notify_by_email and self.recipients:
				try:
					self.send_skip_notification()
				except Exception:
					frappe.log_error(title="Auto Repeat Skip Notification Failed", message=msg)
			return None
		frappe.throw(
			_("Cannot create document: source {0} is cancelled and no valid amendment found").format(
				self.reference_document
			)
		)

	def send_skip_notification(self):
		"""Notify recipients that an Auto Repeat run was skipped due to a cancelled source."""
		if not (self.notify_by_email and self.recipients):
			return
		subject = _("Auto Repeat Skipped: source cancelled — {0}").format(self.name)
		message = _(
			"Auto Repeat <b>{0}</b> was skipped because the source document <b>{1}</b> "
			"is cancelled and no valid amendment was found. The Auto Repeat has been disabled."
		).format(self.name, self.reference_document)
		make(
			doctype=self.doctype,
			name=self.name,
			recipients=self.recipients,
			subject=subject,
			content=message,
			send_email=1,
		)

	# ── Copy-mode refresh helpers ─────────────────────────────────────────

	def refresh_item_prices(self, new_doc):
		"""Refresh item rates / price-list rates from the latest Price List."""
		try:
			from erpnext.stock.get_item_details import get_item_details
		except ImportError:
			frappe.log_error(
				title="Auto Repeat Refresh Skipped",
				message=f"Auto Repeat {self.name}: refresh_prices requires ERPNext.",
			)
			return
		if not new_doc.get("items"):
			return
		posting_date = new_doc.get("posting_date") or new_doc.get("transaction_date") or getdate()
		price_list = new_doc.get("selling_price_list") or new_doc.get("buying_price_list")
		currency = new_doc.get("currency") or new_doc.get("price_list_currency")
		party_field = "customer" if new_doc.get("customer") else ("supplier" if new_doc.get("supplier") else None)
		party = new_doc.get(party_field) if party_field else None
		for item in new_doc.get("items", []):
			if not item.item_code:
				continue
			ctx = frappe._dict(
				{
					"item_code": item.item_code,
					"company": new_doc.get("company"),
					"doctype": new_doc.doctype,
					"posting_date": posting_date,
					"transaction_date": posting_date,
					"price_list": price_list,
					"price_list_currency": currency,
					"currency": currency,
					"qty": item.qty or 1,
					"uom": item.uom,
					"customer": new_doc.get("customer"),
					"supplier": new_doc.get("supplier"),
					"warehouse": item.get("warehouse"),
					"conversion_rate": new_doc.get("conversion_rate") or 1,
					"plc_conversion_rate": new_doc.get("plc_conversion_rate") or 1,
					"ignore_pricing_rule": 0 if self.apply_pricing_rules else 1,
					"transaction_type": "selling" if party_field == "customer" else "buying",
				}
			)
			try:
				details = get_item_details(ctx)
				if details.get("price_list_rate") is not None:
					item.price_list_rate = flt(details.price_list_rate)
				if details.get("rate") is not None:
					item.rate = flt(details.rate)
				if details.get("discount_percentage") is not None:
					item.discount_percentage = flt(details.discount_percentage)
			except Exception as e:
				frappe.log_error(
					title="Auto Repeat Price Refresh Failed",
					message=f"Auto Repeat {self.name}: row {item.idx} ({item.item_code}) — {e}",
				)

	def refresh_conversion_rate(self, new_doc):
		"""Refresh conversion_rate using the FX rate at the new posting date."""
		try:
			from erpnext.setup.utils import get_exchange_rate
		except ImportError:
			frappe.log_error(
				title="Auto Repeat Refresh Skipped",
				message=f"Auto Repeat {self.name}: refresh_exchange_rate requires ERPNext.",
			)
			return
		if not new_doc.get("currency") or not new_doc.get("company"):
			return
		company_currency = frappe.get_cached_value("Company", new_doc.company, "default_currency")
		if new_doc.currency == company_currency:
			return
		posting_date = new_doc.get("posting_date") or new_doc.get("transaction_date") or getdate()
		try:
			rate = get_exchange_rate(new_doc.currency, company_currency, posting_date)
		except Exception as e:
			frappe.log_error(
				title="Auto Repeat FX Refresh Failed",
				message=f"Auto Repeat {self.name}: {e}",
			)
			return
		if rate:
			new_doc.conversion_rate = flt(rate)
			if new_doc.meta.has_field("plc_conversion_rate"):
				new_doc.plc_conversion_rate = flt(rate)

	def refresh_sales_tax_template_for(self, new_doc):
		applicable = ("Sales Invoice", "Sales Order", "Quotation", "Delivery Note")
		if new_doc.doctype not in applicable:
			return
		template = None
		if new_doc.get("customer"):
			template = frappe.db.get_value("Customer", new_doc.customer, "default_taxes_and_charges")
		if not template and new_doc.get("company"):
			template = frappe.db.get_value(
				"Sales Taxes and Charges Template",
				{"company": new_doc.company, "is_default": 1, "disabled": 0},
				"name",
			)
		self._apply_taxes_template(new_doc, template)

	def refresh_purchase_tax_template_for(self, new_doc):
		applicable = ("Purchase Invoice", "Purchase Order", "Purchase Receipt")
		if new_doc.doctype not in applicable:
			return
		template = None
		if new_doc.get("supplier"):
			template = frappe.db.get_value("Supplier", new_doc.supplier, "default_taxes_and_charges")
		if not template and new_doc.get("company"):
			template = frappe.db.get_value(
				"Purchase Taxes and Charges Template",
				{"company": new_doc.company, "is_default": 1, "disabled": 0},
				"name",
			)
		self._apply_taxes_template(new_doc, template)

	def _apply_taxes_template(self, new_doc, template):
		if not template or new_doc.get("taxes_and_charges") == template:
			return
		new_doc.taxes_and_charges = template
		# Reset taxes table; controller's calculate_taxes_and_totals/get_taxes
		# will repopulate from the template at insert/save time.
		new_doc.set("taxes", [])
		try:
			tax_rows = frappe.get_all(
				"Sales Taxes and Charges"
				if new_doc.doctype in ("Sales Invoice", "Sales Order", "Quotation", "Delivery Note")
				else "Purchase Taxes and Charges",
				filters={"parent": template},
				fields="*",
				order_by="idx asc",
			)
			for row in tax_rows:
				row.pop("name", None)
				row.pop("parent", None)
				row.pop("parenttype", None)
				row.pop("parentfield", None)
				new_doc.append("taxes", row)
		except Exception as e:
			frappe.log_error(
				title="Auto Repeat Tax Template Refresh Failed",
				message=f"Auto Repeat {self.name}: template {template} — {e}",
			)

	def refresh_item_tax_template_for(self, new_doc):
		"""Re-derive each item row's Item Tax Template from the Item master.

		Respects valid_from <= posting_date — see WP §4.2 GAP-027.
		Clears stale per-item exemptions when the Item master no longer has
		an applicable template.
		"""
		if not new_doc.get("items"):
			return
		posting_date = new_doc.get("posting_date") or new_doc.get("transaction_date") or getdate()
		for item in new_doc.get("items", []):
			if not item.item_code:
				continue
			applicable = frappe.get_all(
				"Item Tax",
				filters={
					"parent": item.item_code,
					"parenttype": "Item",
					"valid_from": ["<=", posting_date],
				},
				fields=["item_tax_template"],
				order_by="valid_from desc",
				limit=1,
			)
			item.item_tax_template = applicable[0].item_tax_template if applicable else None

	def refresh_shipping_rule_for(self, new_doc):
		applicable = ("Sales Invoice", "Sales Order", "Delivery Note", "Quotation")
		if new_doc.doctype not in applicable or not new_doc.get("shipping_rule"):
			return
		try:
			rule = frappe.get_doc("Shipping Rule", new_doc.shipping_rule)
			rule.apply(new_doc)
		except Exception as e:
			frappe.log_error(
				title="Auto Repeat Shipping Rule Refresh Failed",
				message=f"Auto Repeat {self.name}: rule {new_doc.shipping_rule} — {e}",
			)

	def recalculate_document_taxes(self, new_doc):
		if hasattr(new_doc, "calculate_taxes_and_totals"):
			try:
				new_doc.calculate_taxes_and_totals()
			except Exception as e:
				frappe.log_error(
					title="Auto Repeat Tax Recalc Failed",
					message=f"Auto Repeat {self.name}: {e}",
				)
		else:
			frappe.log_error(
				title="Auto Repeat Tax Recalc Skipped",
				message=f"Auto Repeat {self.name}: {new_doc.doctype} has no calculate_taxes_and_totals().",
			)

	def recalculate_payment_schedule(self, new_doc):
		if hasattr(new_doc, "set_payment_schedule"):
			try:
				new_doc.set_payment_schedule()
			except Exception as e:
				frappe.log_error(
					title="Auto Repeat Payment Schedule Recalc Failed",
					message=f"Auto Repeat {self.name}: {e}",
				)

	def apply_cost_center_allocation(self, new_doc):
		"""Audit Cost Center Allocation rules for the new posting date.

		Document-level cost centers are NOT mutated; the GL distribution that ERPNext
		performs in general_ledger.distribute_gl_based_on_cost_center_allocation() at
		posting time uses the new posting date directly. We log an audit row whenever
		a row's cost center has any allocation valid for the new posting date so the
		operator can verify the resulting split.
		"""
		try:
			from erpnext.accounts.general_ledger import get_cost_center_allocation_data
		except ImportError:
			return
		company = new_doc.get("company")
		posting_date = new_doc.get("posting_date") or new_doc.get("transaction_date") or getdate()
		if not company:
			return
		rows = []
		if new_doc.get("items"):
			rows.extend(new_doc.get("items"))
		if new_doc.get("accounts"):
			rows.extend(new_doc.get("accounts"))
		seen = set()
		for row in rows:
			cc = row.get("cost_center")
			if not cc or cc in seen:
				continue
			seen.add(cc)
			try:
				allocation = get_cost_center_allocation_data(company, posting_date, cc)
			except Exception:
				continue
			if allocation:
				frappe.log_error(
					title="Auto Repeat Cost Center Allocation",
					message=(
						f"Auto Repeat {self.name}: cost center {cc} has allocation rules "
						f"valid for {posting_date}. GL distribution will apply at posting time."
					),
				)

	# ── Reversal-mode helpers ─────────────────────────────────────────────

	def make_reversal_document(self, reference_doc):
		"""Create a reversal Journal Entry from the source via ERPNext's existing helper.

		Single-execution: the Auto Repeat is disabled after one successful insert,
		regardless of whether auto_submit_reversal succeeded.
		"""
		try:
			from erpnext.accounts.doctype.journal_entry.journal_entry import (
				make_reverse_journal_entry,
			)
		except ImportError:
			frappe.throw(_("Reversal mode requires the ERPNext app to be installed"))

		# GA-0001-01 already exposes is_reversed on the source; if the source has been
		# reversed by hand (or by a previous AR run), we never recurse — log + disable.
		if getattr(reference_doc, "is_reversed", 0):
			frappe.log_error(
				title="Auto Repeat Skipped",
				message=f"Auto Repeat {self.name}: source {reference_doc.name} already reversed.",
			)
			self.db_set("disabled", 1)
			self.db_set("status", "Completed")
			if reference_doc.doctype == "Journal Entry" and reference_doc.meta.has_field(
				"auto_reversal_status"
			):
				frappe.db.set_value(
					"Journal Entry", reference_doc.name, "auto_reversal_status", "Cancelled"
				)
			return None

		reversal = make_reverse_journal_entry(reference_doc.name)

		# Schedule
		if self.reverse_on_next_month:
			reversal.posting_date = get_first_day(add_months(getdate(), 1))
		elif self.reverse_date:
			reversal.posting_date = getdate(self.reverse_date)

		# WP GAP-013/014 — make_reverse_journal_entry does not copy cost_center / party / project.
		self.enhance_reversal_mapping(reversal, reference_doc)

		# WP GAP-021 / Phase 5.1 — FX handling
		if self.reversal_exchange_rate_type == "Current Rate":
			self.refresh_reversal_exchange_rate(reversal, reference_doc)

		# WP GAP-022 — cost-center allocation audit
		if self.reversal_cost_center_mode == "Apply Current Allocation":
			self.apply_reversal_cost_center_allocation(reversal)

		# WP GAP-021 — tax recalculation (audit-only — see imp plan §6 sign-off #4)
		if self.reversal_tax_mode == "Recalculate for Posting Date":
			self.recalculate_reversal_taxes(reversal)

		reversal.user_remark = (reversal.user_remark or "") + (
			f"\nAuto-created by Auto Repeat {self.name}".strip()
		)
		reversal.flags.ignore_permissions = True
		reversal.flags.updater_reference = {
			"doctype": self.doctype,
			"docname": self.name,
			"label": _("via Auto Repeat (Reversal)"),
		}
		reversal.insert()

		# Wire the JE-side status fields if ERPNext has them (added by GA-0001-05+06 ERPNext PR)
		je_meta = frappe.get_meta("Journal Entry")
		updates = {}
		if je_meta.has_field("linked_auto_repeat"):
			updates["linked_auto_repeat"] = self.name
		if je_meta.has_field("auto_reversal_status"):
			updates["auto_reversal_status"] = "Scheduled"
		if updates:
			frappe.db.set_value("Journal Entry", reference_doc.name, updates)

		if self.auto_submit_reversal:
			try:
				reversal.submit()
				if je_meta.has_field("auto_reversal_status"):
					frappe.db.set_value(
						"Journal Entry", reference_doc.name, "auto_reversal_status", "Completed"
					)
			except Exception:
				if je_meta.has_field("auto_reversal_status"):
					frappe.db.set_value(
						"Journal Entry", reference_doc.name, "auto_reversal_status", "Failed"
					)
				raise

		# Single-execution semantics — disable after first successful insert
		self.db_set("disabled", 1)
		self.db_set("status", "Completed")
		return reversal

	def enhance_reversal_mapping(self, reversal, original):
		"""Copy cost_center / party / project / user_remark per-row.

		make_reverse_journal_entry's field_map only swaps debit ↔ credit; everything
		else needs explicit copying. We pair rows positionally because the field_map
		preserves row order.
		"""
		for i, row in enumerate(reversal.accounts):
			if i >= len(original.accounts):
				break
			orig = original.accounts[i]
			row.cost_center = orig.get("cost_center")
			row.project = orig.get("project")
			row.party_type = orig.get("party_type")
			row.party = orig.get("party")
			if not row.user_remark and orig.get("user_remark"):
				row.user_remark = orig.user_remark

	def refresh_reversal_exchange_rate(self, reversal, original):
		"""Revalue the reversal at the FX rate of the reversal posting date.

		Breaks immutable-ledger compliance — the operator was warned at save time.
		"""
		try:
			from erpnext.setup.utils import get_exchange_rate
		except ImportError:
			return
		if not getattr(reversal, "multi_currency", 0):
			return
		posting_date = reversal.posting_date or getdate()
		company_currency = frappe.get_cached_value(
			"Company", reversal.company, "default_currency"
		)
		for row in reversal.accounts:
			if row.account_currency and row.account_currency != company_currency:
				try:
					new_rate = get_exchange_rate(
						row.account_currency, company_currency, posting_date
					)
				except Exception:
					continue
				if new_rate:
					row.exchange_rate = flt(new_rate)
					row.debit = flt(row.debit_in_account_currency) * flt(new_rate)
					row.credit = flt(row.credit_in_account_currency) * flt(new_rate)
		if hasattr(reversal, "set_total_debit_credit"):
			reversal.set_total_debit_credit()

	def apply_reversal_cost_center_allocation(self, reversal):
		"""Audit Cost Center Allocation rules for the reversal posting date."""
		try:
			from erpnext.accounts.general_ledger import get_cost_center_allocation_data
		except ImportError:
			return
		posting_date = reversal.posting_date or getdate()
		company = reversal.company
		seen = set()
		for row in reversal.accounts:
			if not row.cost_center or row.cost_center in seen:
				continue
			seen.add(row.cost_center)
			try:
				allocation = get_cost_center_allocation_data(
					company, posting_date, row.cost_center
				)
			except Exception:
				continue
			if allocation:
				frappe.log_error(
					title="Auto Repeat Reversal Cost Center Allocation",
					message=(
						f"Auto Repeat {self.name}: reversal cost center {row.cost_center} "
						f"has allocation rules for {posting_date}. "
						f"GL distribution will apply at posting time: {allocation}"
					),
				)

	def recalculate_reversal_taxes(self, reversal):
		"""Audit-log per-tax-account when 'Recalculate for Posting Date' is selected.

		Per WP §4.2 GAP-021 (status CLOSED) and imp-plan sign-off #4: actual amount
		recomputation on a JE reversal is genuinely complex and is deferred. The
		setting exists for the audit trail; we record a log row per tax account so
		Finance can review whether manual adjustment is needed.
		"""
		for row in reversal.accounts:
			if not row.account:
				continue
			account_type = frappe.get_cached_value("Account", row.account, "account_type")
			if account_type == "Tax":
				frappe.log_error(
					title="Auto Repeat Reversal Tax Note",
					message=(
						f"Auto Repeat {self.name}: tax account {row.account} on the reversal "
						f"keeps the original amount. If tax rates changed between the original "
						f"and reversal posting dates, manual adjustment may be required."
					),
				)


def get_next_date(dt, mcount, day=None):
	dt = getdate(dt)
	dt += relativedelta(months=mcount, day=day)
	return dt


def get_next_weekday(current_schedule_day, weekdays):
	days = list(week_map.keys())
	if current_schedule_day > 0:
		days = days[(current_schedule_day + 1) :] + days[:current_schedule_day]
	else:
		days = days[(current_schedule_day + 1) :]

	for entry in days:
		if entry in weekdays:
			return entry


# called through hooks
def make_auto_repeat_entry():
	enqueued_method = "frappe.automation.doctype.auto_repeat.auto_repeat.create_repeated_entries"
	jobs = get_jobs()

	if not jobs or enqueued_method not in jobs[frappe.local.site]:
		date = getdate(today())
		data = get_auto_repeat_entries(date)
		frappe.enqueue(enqueued_method, data=data, queue="long")


def create_repeated_entries(data):
	for d in data:
		doc = frappe.get_doc("Auto Repeat", d.name)

		current_date = getdate(today())
		schedule_date = getdate(doc.next_schedule_date)

		if schedule_date == current_date and not doc.disabled:
			doc.create_documents()
			schedule_date = doc.get_next_schedule_date(schedule_date=schedule_date)
			if schedule_date and not doc.disabled:
				frappe.db.set_value("Auto Repeat", doc.name, "next_schedule_date", schedule_date)

		if doc.is_completed():
			doc.status = "Completed"
			doc.save()


def get_auto_repeat_entries(date=None):
	if not date:
		date = getdate(today())

	auto_repeat = frappe.qb.DocType("Auto Repeat")
	query = frappe.qb.from_(auto_repeat)
	query = query.select("name")
	query = query.where(
		(auto_repeat.next_schedule_date <= date)
		& (auto_repeat.status == "Active")
		& ((auto_repeat.end_date >= auto_repeat.next_schedule_date) | (auto_repeat.end_date.isnull()))
	)
	return query.run(as_dict=1)


@frappe.whitelist()
def make_auto_repeat(doctype, docname, frequency="Daily", start_date=None, end_date=None):
	if not start_date:
		start_date = getdate(today())
	doc = frappe.new_doc("Auto Repeat")
	doc.reference_doctype = doctype
	doc.reference_document = docname
	doc.frequency = frequency
	doc.start_date = start_date
	if end_date:
		doc.end_date = end_date
	doc.save()
	return doc


# method for reference_doctype filter
@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_auto_repeat_doctypes(doctype, txt, searchfield, start, page_len, filters):
	res = frappe.get_all(
		"Property Setter",
		{
			"property": "allow_auto_repeat",
			"value": "1",
		},
		["doc_type"],
	)
	docs = [r.doc_type for r in res]

	res = frappe.get_all(
		"DocType",
		{
			"allow_auto_repeat": 1,
		},
		["name"],
	)
	docs += [r.name for r in res]
	docs = set(list(docs))

	return [[d] for d in docs if txt in d]


@frappe.whitelist()
def update_reference(docname: str, reference: str):
	doc = frappe.get_doc("Auto Repeat", str(docname))
	doc.check_permission("write")
	doc.db_set("reference_document", str(reference))
	return "success"  # backward compatbility


@frappe.whitelist(methods=["POST"])
def generate_message_preview(name: str):
	frappe.has_permission("Auto Repeat", "write", throw=True)
	auto_repeat = frappe.get_doc("Auto Repeat", str(name))
	doc = frappe.get_doc(auto_repeat.reference_doctype, auto_repeat.reference_document)
	doc.check_permission()
	subject_preview = _("Please add a subject to your email")
	msg_preview = frappe.render_template(auto_repeat.message, {"doc": doc})
	if auto_repeat.subject:
		subject_preview = frappe.render_template(auto_repeat.subject, {"doc": doc})

	return {"message": msg_preview, "subject": subject_preview}
