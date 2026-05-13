// Copyright (c) 2018, Frappe Technologies and contributors
// For license information, please see license.txt
frappe.provide("frappe.auto_repeat");

frappe.ui.form.on("Auto Repeat", {
	setup: function (frm) {
		frm.fields_dict["reference_doctype"].get_query = function () {
			return {
				query: "frappe.automation.doctype.auto_repeat.auto_repeat.get_auto_repeat_doctypes",
			};
		};

		frm.fields_dict["reference_document"].get_query = function () {
			return {
				filters: {
					auto_repeat: "",
				},
			};
		};

		frm.fields_dict["print_format"].get_query = function () {
			return {
				filters: {
					doc_type: frm.doc.reference_doctype,
				},
			};
		};
	},

	refresh: function (frm) {
		// auto repeat message
		if (frm.is_new()) {
			let customize_form_link = `<a href="/desk/customize-form">${__("Customize Form")}</a>`;
			frm.dashboard.set_headline(
				__('To configure Auto Repeat, enable "Allow Auto Repeat" from {0}.', [
					customize_form_link,
				])
			);
		}

		// view document button
		if (!frm.is_dirty()) {
			let label = __("View {0}", [__(frm.doc.reference_doctype)]);
			frm.add_custom_button(label, () =>
				frappe.set_route("List", frm.doc.reference_doctype, { auto_repeat: frm.doc.name })
			);
		}

		// auto repeat schedule (Copy mode only — Reversal mode is single-execution)
		if (frm.doc.repeat_type !== "Reversal") {
			frappe.auto_repeat.render_schedule(frm);
		} else {
			frm.dashboard.hide();
			frappe.auto_repeat.show_reversal_indicator(frm);
		}

		frm.trigger("toggle_submit_on_creation");
	},

	reference_doctype: function (frm) {
		frm.trigger("toggle_submit_on_creation");
	},

	repeat_type: function (frm) {
		// Reversal-mode config (reverse_on_next_month / reverse_date /
		// reversal_*) and Copy-mode refresh switches (refresh_prices /
		// recalculate_taxes / refresh_*_template / etc.) all moved off the
		// AR doctype in the upstream-shape refactor (Phases C + D,
		// WP GA-0001-05+06). The consuming app owns both via registered
		// handlers, so there's no AR-side state to clear when toggling.
		if (frm.doc.repeat_type === "Reversal") {
			// frequency is not required in Reversal mode — clear stale value
			frm.set_value("frequency", "");
		}
	},

	toggle_submit_on_creation: function (frm) {
		// submit on creation checkbox
		if (frm.doc.reference_doctype) {
			frappe.model.with_doctype(frm.doc.reference_doctype, () => {
				let meta = frappe.get_meta(frm.doc.reference_doctype);
				frm.toggle_display("submit_on_creation", meta.is_submittable);
			});
		}
	},

	template: function (frm) {
		if (frm.doc.template) {
			frappe.model.with_doc("Email Template", frm.doc.template, () => {
				let email_template = frappe.get_doc("Email Template", frm.doc.template);
				frm.set_value("subject", email_template.subject);
				let message_value = email_template.response;
				if (email_template.use_html) message_value = email_template.response_html;
				frm.set_value("message", message_value);
				frm.refresh_field("subject");
				frm.refresh_field("message");
			});
		}
	},

	get_contacts: function (frm) {
		frm.call("fetch_linked_contacts");
	},

	preview_message: function (frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Please save the form before previewing the message"));
			return;
		}

		if (frm.doc.message) {
			frappe.call({
				method: "frappe.automation.doctype.auto_repeat.auto_repeat.generate_message_preview",
				type: "POST",
				args: {
					name: frm.doc.name,
				},
				callback: function (r) {
					if (r.message) {
						frappe.msgprint(r.message.message, r.message.subject);
					}
				},
			});
		} else {
			frappe.msgprint(__("Please setup a message first"), __("Message not setup"));
		}
	},
});

frappe.auto_repeat.show_reversal_indicator = function (frm) {
	if (frm.is_new()) return;
	let label, color;
	if (frm.doc.status === "Completed") {
		label = __("Reversal Completed");
		color = "green";
	} else if (frm.doc.disabled) {
		label = __("Reversal Cancelled");
		color = "grey";
	} else if (frm.doc.next_schedule_date) {
		label = __("Reversal Scheduled — {0}", [frappe.datetime.str_to_user(frm.doc.next_schedule_date)]);
		color = "blue";
	} else {
		label = __("Reversal Mode");
		color = "blue";
	}
	frm.dashboard.add_indicator(label, color);
};

frappe.auto_repeat.render_schedule = function (frm) {
	if (!frm.is_dirty() && frm.doc.status !== "Disabled") {
		frm.call("get_auto_repeat_schedule").then((r) => {
			frm.dashboard.reset();
			frm.dashboard.add_section(
				frappe.render_template("auto_repeat_schedule", {
					schedule_details: r.message || [],
				}),
				__("Auto Repeat Schedule")
			);
			frm.dashboard.show();
		});
	} else {
		frm.dashboard.hide();
	}
};
