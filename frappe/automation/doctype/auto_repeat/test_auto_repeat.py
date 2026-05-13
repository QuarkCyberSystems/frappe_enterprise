# Copyright (c) 2018, Frappe Technologies and Contributors
# License: MIT. See LICENSE
from typing import TYPE_CHECKING

import frappe
from frappe.automation.doctype.auto_repeat.auto_repeat import (
	create_repeated_entries,
	get_auto_repeat_entries,
	week_map,
)
from frappe.custom.doctype.custom_field.custom_field import create_custom_field
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_months, getdate, today

if TYPE_CHECKING:
	from frappe.custom.doctype.custom_field.custom_field import CustomField


def add_custom_fields() -> "CustomField":
	df = dict(
		fieldname="auto_repeat",
		label="Auto Repeat",
		fieldtype="Link",
		insert_after="sender",
		options="Auto Repeat",
		hidden=1,
		print_hide=1,
		read_only=1,
	)
	return create_custom_field("ToDo", df) or frappe.get_doc(
		"Custom Field", dict(fieldname=df["fieldname"], dt="ToDo")
	)


class TestAutoRepeat(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		cls.custom_field = add_custom_fields()
		cls.addClassCleanup(cls.custom_field.delete)
		return super().setUpClass()

	def test_daily_auto_repeat(self):
		todo = frappe.get_doc(
			doctype="ToDo", description="test recurring todo", assigned_by="Administrator"
		).insert()

		doc = make_auto_repeat(reference_document=todo.name)
		self.assertEqual(doc.next_schedule_date, today())
		data = get_auto_repeat_entries(getdate(today()))
		create_repeated_entries(data)
		frappe.db.commit()

		todo = frappe.get_doc(doc.reference_doctype, doc.reference_document)
		self.assertEqual(todo.auto_repeat, doc.name)

		new_todo = frappe.db.get_value("ToDo", {"auto_repeat": doc.name, "name": ("!=", todo.name)}, "name")

		new_todo = frappe.get_doc("ToDo", new_todo)

		self.assertEqual(todo.get("description"), new_todo.get("description"))

	def test_weekly_auto_repeat(self):
		todo = frappe.get_doc(
			doctype="ToDo", description="test weekly todo", assigned_by="Administrator"
		).insert()

		doc = make_auto_repeat(
			reference_doctype="ToDo",
			frequency="Weekly",
			reference_document=todo.name,
			start_date=add_days(today(), -7),
		)

		self.assertEqual(doc.next_schedule_date, today())
		data = get_auto_repeat_entries(getdate(today()))
		create_repeated_entries(data)
		frappe.db.commit()

		todo = frappe.get_doc(doc.reference_doctype, doc.reference_document)
		self.assertEqual(todo.auto_repeat, doc.name)

		new_todo = frappe.db.get_value("ToDo", {"auto_repeat": doc.name, "name": ("!=", todo.name)}, "name")

		new_todo = frappe.get_doc("ToDo", new_todo)

		self.assertEqual(todo.get("description"), new_todo.get("description"))

	def test_fortnightly_auto_repeat(self):
		todo = frappe.get_doc(
			doctype="ToDo", description="test fortnightly todo", assigned_by="Administrator"
		).insert()

		doc = make_auto_repeat(
			reference_doctype="ToDo",
			frequency="Fortnightly",
			reference_document=todo.name,
			start_date=add_days(today(), -14),
		)

		self.assertEqual(doc.next_schedule_date, today())
		data = get_auto_repeat_entries(getdate(today()))
		create_repeated_entries(data)
		frappe.db.commit()

		todo = frappe.get_doc(doc.reference_doctype, doc.reference_document)
		self.assertEqual(todo.auto_repeat, doc.name)

		new_todo = frappe.db.get_value("ToDo", {"auto_repeat": doc.name, "name": ("!=", todo.name)}, "name")

		new_todo = frappe.get_doc("ToDo", new_todo)

		self.assertEqual(todo.get("description"), new_todo.get("description"))

	def test_weekly_auto_repeat_with_weekdays(self):
		todo = frappe.get_doc(
			doctype="ToDo", description="test auto repeat with weekdays", assigned_by="Administrator"
		).insert()

		weekdays = list(week_map.keys())
		current_weekday = getdate().weekday()
		days = [{"day": weekdays[current_weekday]}, {"day": weekdays[(current_weekday + 2) % 7]}]
		doc = make_auto_repeat(
			reference_doctype="ToDo",
			frequency="Weekly",
			reference_document=todo.name,
			start_date=add_days(today(), -7),
			days=days,
		)

		self.assertEqual(doc.next_schedule_date, today())
		data = get_auto_repeat_entries(getdate(today()))
		create_repeated_entries(data)
		frappe.db.commit()

		todo = frappe.get_doc(doc.reference_doctype, doc.reference_document)
		self.assertEqual(todo.auto_repeat, doc.name)

		doc.reload()
		self.assertEqual(doc.next_schedule_date, add_days(getdate(), 2))

	def test_monthly_auto_repeat(self):
		start_date = today()
		end_date = add_months(start_date, 12)

		todo = frappe.get_doc(
			doctype="ToDo", description="test recurring todo", assigned_by="Administrator"
		).insert()

		self.monthly_auto_repeat("ToDo", todo.name, start_date, end_date)
		# test without end_date
		todo = frappe.get_doc(
			doctype="ToDo", description="test recurring todo without end_date", assigned_by="Administrator"
		).insert()
		self.monthly_auto_repeat("ToDo", todo.name, start_date)

	def monthly_auto_repeat(self, doctype, docname, start_date, end_date=None):
		def get_months(start, end):
			diff = (12 * end.year + end.month) - (12 * start.year + start.month)
			return diff + 1

		doc = make_auto_repeat(
			reference_doctype=doctype,
			frequency="Monthly",
			reference_document=docname,
			start_date=start_date,
			end_date=end_date,
		)

		doc.disable_auto_repeat()

		data = get_auto_repeat_entries(getdate(today()))
		create_repeated_entries(data)
		docnames = frappe.get_all(doc.reference_doctype, {"auto_repeat": doc.name})
		self.assertEqual(len(docnames), 1)

		doc = frappe.get_doc("Auto Repeat", doc.name)
		doc.db_set("disabled", 0)

		months = get_months(getdate(start_date), getdate(today()))
		data = get_auto_repeat_entries(getdate(today()))
		create_repeated_entries(data)

		docnames = frappe.get_all(doc.reference_doctype, {"auto_repeat": doc.name})
		self.assertEqual(len(docnames), months)

	def test_email_notification(self):
		todo = frappe.get_doc(
			doctype="ToDo", description="Test recurring notification attachment", assigned_by="Administrator"
		).insert()

		doc = make_auto_repeat(
			reference_document=todo.name,
			notify=1,
			recipients="test@domain.com",
			subject="New ToDo",
			message="A new ToDo has just been created for you",
		)
		data = get_auto_repeat_entries(getdate(today()))
		create_repeated_entries(data)
		frappe.db.commit()

		new_todo = frappe.db.get_value("ToDo", {"auto_repeat": doc.name, "name": ("!=", todo.name)}, "name")

		email_queue = frappe.db.exists("Email Queue", dict(reference_doctype="ToDo", reference_name=new_todo))
		self.assertTrue(email_queue)

	def test_next_schedule_date(self):
		current_date = getdate(today())
		todo = frappe.get_doc(
			doctype="ToDo", description="test next schedule date for monthly", assigned_by="Administrator"
		).insert()
		doc = make_auto_repeat(
			frequency="Monthly", reference_document=todo.name, start_date=add_months(today(), -2)
		)

		# next_schedule_date is set as on or after current date
		# it should not be a previous month's date
		self.assertTrue(doc.next_schedule_date >= current_date)

		todo = frappe.get_doc(
			doctype="ToDo", description="test next schedule date for daily", assigned_by="Administrator"
		).insert()
		doc = make_auto_repeat(
			frequency="Daily", reference_document=todo.name, start_date=add_days(today(), -2)
		)
		self.assertEqual(getdate(doc.next_schedule_date), current_date)

	def test_submit_on_creation(self):
		doctype = "Test Submittable DocType"
		create_submittable_doctype(doctype)

		current_date = getdate()
		submittable_doc = frappe.get_doc(doctype=doctype, test="test submit on creation").insert()
		submittable_doc.submit()
		doc = make_auto_repeat(
			frequency="Daily",
			reference_doctype=doctype,
			reference_document=submittable_doc.name,
			start_date=add_days(current_date, -1),
			submit_on_creation=1,
		)

		data = get_auto_repeat_entries(current_date)
		create_repeated_entries(data)
		docnames = frappe.get_all(
			doc.reference_doctype, filters={"auto_repeat": doc.name}, fields=["docstatus"], limit=1
		)
		self.assertEqual(docnames[0].docstatus, 1)

	def test_auto_repeat_assignee(self):
		todo = frappe.get_doc(
			doctype="ToDo", description="test assignee todo", assigned_by="Administrator"
		).insert()

		doc = make_auto_repeat(reference_document=todo.name)
		doc.update(
			{
				"assignee": [
					{"user": "Administrator"},
					{"user": "Guest"},
				]
			}
		)
		doc.save()
		self.assertEqual(doc.next_schedule_date, today())
		data = get_auto_repeat_entries(getdate(today()))
		create_repeated_entries(data)
		frappe.db.commit()

		todo = frappe.get_doc(doc.reference_doctype, doc.reference_document)
		self.assertEqual(todo.auto_repeat, doc.name)

		new_todo = frappe.db.get_value("ToDo", {"auto_repeat": doc.name, "name": ("!=", todo.name)}, "name")

		new_todo = frappe.get_doc("ToDo", new_todo)
		self.assertEqual(todo.get("description"), new_todo.get("description"))
		self.assertListEqual(
			sorted(list(new_todo.get_assigned_users())),
			sorted(["Administrator", "Guest"]),
		)

	# ──────────────────────────────────────────────────────────────────────
	# WP GA-0001-05+06 — repeat_type / source-resolution / refresh-mode tests.
	# Tests that require ERPNext (Journal Entry, Sales Invoice) live in
	# apps/erpnext/erpnext/accounts/doctype/journal_entry/test_journal_entry.py.
	# ──────────────────────────────────────────────────────────────────────

	def test_repeat_type_default_is_copy(self):
		"""TC-baseline: a freshly-created Auto Repeat defaults to Copy mode."""
		todo = frappe.get_doc(
			doctype="ToDo", description="repeat-type default test", assigned_by="Administrator"
		).insert()
		doc = make_auto_repeat(reference_document=todo.name)
		self.assertEqual(doc.repeat_type, "Copy")

	def test_validate_reversal_requires_handler(self):
		"""TC-006 (post-refactor): Reversal mode requires a registered handler.

		Previously frappe hardcoded `reference_doctype == "Journal Entry"`. After
		the upstream-shape refactor, any doctype can opt-in by registering an
		auto_repeat_handlers entry in its hooks.py — and frappe rejects a
		Reversal-mode AR for any doctype without a handler. Test with a doctype
		(ToDo) that is guaranteed not to have one.
		"""
		todo = frappe.get_doc(
			doctype="ToDo", description="reversal-no-handler test", assigned_by="Administrator"
		).insert()
		ar = frappe.get_doc(
			{
				"doctype": "Auto Repeat",
				"reference_doctype": "ToDo",
				"reference_document": todo.name,
				"repeat_type": "Reversal",
				"start_date": today(),
				"frequency": "",
			}
		)
		with self.assertRaisesRegex(
			frappe.ValidationError, "No Auto Repeat handler registered"
		):
			ar.insert(ignore_permissions=True)

	# test_validate_reversal_requires_schedule was removed in the upstream-
	# shape refactor. The "you must specify a schedule" check is now the
	# consuming app's concern — its handler can raise before frappe schedules
	# anything. For ERPNext's JE handler, see TC-022/023 in the WP-05+06 ERPNext tests.

	def test_skip_cancelled_source_no_amendment(self):
		"""TC-001: Source cancelled, no amendment, skip_if_source_cancelled=1 → AR disabled."""
		create_submittable_doctype("AR Cancel Source Test")
		src = frappe.get_doc({"doctype": "AR Cancel Source Test", "test": "x"}).insert()
		src.submit()
		ar = make_auto_repeat(reference_doctype="AR Cancel Source Test", reference_document=src.name)
		ar.skip_if_source_cancelled = 1
		ar.follow_amendment_chain = 0
		ar.save()
		# Cancel the source
		src.reload()
		src.cancel()
		ar.reload()
		# Trigger run
		ar.create_documents()
		ar.reload()
		self.assertEqual(ar.disabled, 1)
		self.assertEqual(ar.status, "Disabled")

	def test_follow_amendment_chain_to_latest_version(self):
		"""TC-002: Source cancelled but amended; AR resolves to the latest non-cancelled amendment."""
		create_submittable_doctype("AR Amend Source Test")
		src = frappe.get_doc({"doctype": "AR Amend Source Test", "test": "v1"}).insert()
		src.submit()
		ar = make_auto_repeat(reference_doctype="AR Amend Source Test", reference_document=src.name)
		ar.follow_amendment_chain = 1
		ar.save()

		# Amend: cancel + insert successor with amended_from
		src.reload()
		src.cancel()
		amended = frappe.copy_doc(src)
		amended.docstatus = 0  # copy_doc preserves docstatus from cancelled source
		amended.amended_from = src.name
		amended.test = "v2"
		amended.insert()
		amended.submit()

		ar.reload()
		resolved = ar.get_authoritative_source()
		self.assertIsNotNone(resolved)
		self.assertEqual(resolved.name, amended.name)
		ar.reload()
		self.assertEqual(ar.current_source_document, amended.name)

	def test_amendment_chain_walks_multiple_steps(self):
		"""TC-002b: Multi-hop amendment chain resolves to the latest non-cancelled successor."""
		create_submittable_doctype("AR Multi Amend Test")
		v1 = frappe.get_doc({"doctype": "AR Multi Amend Test", "test": "v1"}).insert()
		v1.submit()
		ar = make_auto_repeat(reference_doctype="AR Multi Amend Test", reference_document=v1.name)
		ar.follow_amendment_chain = 1
		ar.save()

		# v1 → v2 → v3 — only v3 should remain non-cancelled
		v1.reload()
		v1.cancel()
		v2 = frappe.copy_doc(v1)
		v2.docstatus = 0
		v2.amended_from = v1.name
		v2.test = "v2"
		v2.insert()
		v2.submit()
		v2.reload()
		v2.cancel()
		v3 = frappe.copy_doc(v2)
		v3.docstatus = 0
		v3.amended_from = v2.name
		v3.test = "v3"
		v3.insert()
		v3.submit()

		ar.reload()
		resolved = ar.get_authoritative_source()
		self.assertEqual(resolved.name, v3.name)

	def test_skip_cancelled_source_returns_none_when_no_amendment(self):
		"""TC-001b: get_authoritative_source returns None when source cancelled and no amendment."""
		create_submittable_doctype("AR No Amend Test")
		src = frappe.get_doc({"doctype": "AR No Amend Test", "test": "x"}).insert()
		src.submit()
		ar = make_auto_repeat(reference_doctype="AR No Amend Test", reference_document=src.name)
		ar.follow_amendment_chain = 1
		ar.save()
		src.reload()
		src.cancel()
		ar.reload()
		self.assertIsNone(ar.get_authoritative_source())

	def test_set_dates_reversal_uses_start_date(self):
		"""TC-005 (post-refactor): Reversal mode sets next_schedule_date = start_date.

		Doctype-specific schedule semantics (JE's "First Day of Next Month" vs
		Specific Date) are computed by the consuming app before AR creation;
		the app passes the resolved date as `start_date`. Frappe's set_dates
		then just propagates that to next_schedule_date for the cron to pick up.
		"""
		if not frappe.db.exists("DocType", "Journal Entry"):
			self.skipTest("Journal Entry doctype not available — Frappe-only site")
		target = add_days(today(), 14)
		ar = frappe.new_doc("Auto Repeat")
		ar.update(
			{
				"reference_doctype": "Journal Entry",
				"reference_document": "JE-DUMMY",
				"repeat_type": "Reversal",
				"start_date": target,
				"frequency": "",
			}
		)
		ar.set_dates()
		self.assertEqual(getdate(ar.next_schedule_date), getdate(target))

	def test_copy_mode_unchanged_when_refresh_switches_off(self):
		"""Regression: with all refresh switches off, Copy mode behaves identically to today."""
		todo = frappe.get_doc(
			doctype="ToDo", description="copy-mode regression test", assigned_by="Administrator"
		).insert()
		doc = make_auto_repeat(reference_document=todo.name)
		# All new switches default off; refresh_mode default "Copy Original"
		self.assertEqual(doc.repeat_type, "Copy")
		self.assertEqual(doc.refresh_mode, "Copy Original")
		self.assertEqual(doc.refresh_prices, 0)
		self.assertEqual(doc.refresh_exchange_rate, 0)
		self.assertEqual(doc.recalculate_taxes, 0)
		# Should run as before
		data = get_auto_repeat_entries(getdate(today()))
		create_repeated_entries(data)
		frappe.db.commit()
		new_todo = frappe.db.get_value(
			"ToDo", {"auto_repeat": doc.name, "name": ("!=", todo.name)}, "name"
		)
		self.assertIsNotNone(new_todo)

	def test_auto_repeat_assignee_with_separate_documents(self):
		todo = frappe.get_doc(
			doctype="ToDo",
			description="test assignee todo with multiple doc",
			assigned_by="Administrator",
		).insert()

		doc = make_auto_repeat(reference_document=todo.name)
		doc.update(
			{
				"assignee": [
					{"user": "Administrator"},
					{"user": "Guest"},
				],
				"generate_separate_documents_for_each_assignee": 1,
			}
		)
		doc.save()
		self.assertEqual(doc.next_schedule_date, today())
		data = get_auto_repeat_entries(getdate(today()))
		create_repeated_entries(data)
		frappe.db.commit()

		todo = frappe.get_doc(doc.reference_doctype, doc.reference_document)
		self.assertEqual(todo.auto_repeat, doc.name)

		new_todo_count = frappe.db.count("ToDo", {"auto_repeat": doc.name, "name": ("!=", todo.name)}, "name")

		self.assertEqual(new_todo_count, 2)


def make_auto_repeat(**args):
	args = frappe._dict(args)
	return frappe.get_doc(
		{
			"doctype": "Auto Repeat",
			"reference_doctype": args.reference_doctype or "ToDo",
			"reference_document": args.reference_document or frappe.db.get_value("ToDo", "name"),
			"submit_on_creation": args.submit_on_creation or 0,
			"frequency": args.frequency or "Daily",
			"start_date": args.start_date or add_days(today(), -1),
			"end_date": args.end_date or "",
			"notify_by_email": args.notify or 0,
			"recipients": args.recipients or "",
			"subject": args.subject or "",
			"message": args.message or "",
			"repeat_on_days": args.days or [],
		}
	).insert(ignore_permissions=True)


def create_submittable_doctype(doctype, submit_perms=1):
	if frappe.db.exists("DocType", doctype):
		return
	else:
		doc = frappe.get_doc(
			{
				"doctype": "DocType",
				"__newname": doctype,
				"module": "Custom",
				"custom": 1,
				"is_submittable": 1,
				"fields": [{"fieldname": "test", "label": "Test", "fieldtype": "Data"}],
				"permissions": [
					{
						"role": "System Manager",
						"read": 1,
						"write": 1,
						"create": 1,
						"delete": 1,
						"submit": submit_perms,
						"cancel": submit_perms,
						"amend": submit_perms,
					}
				],
			}
		).insert()

		doc.allow_auto_repeat = 1
		doc.save()
