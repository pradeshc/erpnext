# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from __future__ import unicode_literals
import frappe, erpnext

import traceback

from erpnext import get_company_currency, get_default_company
from erpnext.accounts.report.utils import get_currency, convert_to_presentation_currency
from frappe.utils import getdate, cstr, flt, fmt_money
from frappe import _, _dict
from erpnext.accounts.utils import get_account_currency
from erpnext.accounts.report.financial_statements import get_cost_centers_with_children
from six import iteritems
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions
from collections import OrderedDict

from frappe.contacts.doctype.address.address import get_address_display
from frappe.desk.reportview import build_match_conditions

# import datetime
from datetime import date


def execute(filters=None):
	if not filters:
		return [], []
	
	header = {}

	account_details = {}

	if filters and filters.get('print_in_account_currency') and \
		not filters.get('account'):
		frappe.throw(_("Select an account to print in account currency"))

	for acc in frappe.db.sql("""select name, is_group from tabAccount""", as_dict=1):
		account_details.setdefault(acc.name, acc)

	validate_filters(filters, account_details)

	validate_party(filters)
	header["customer_name"] = filters.party
	header["customer_details"] = get_customer_details(filters.party)
	
	filters = set_account_currency(filters)

	columns = get_columns(filters)

	res = get_result(filters, account_details)
	
	current, days30, days60, days90  = ageing_bucket_totals(res)
	
	header["current"] = current
	header["days30"]  = days30
	header["days60"]  = days60
	header["days90"]  = days90

	return columns, res, None, None, header


def get_customer_details(party):
	
	if not party:
		return []

	address = frappe.db.sql(""" 
		SELECT name, address_line1, address_line2, city, state,
			   email_id, phone, fax, pincode 
		FROM  `tabAddress`
		WHERE is_shipping_address = 0
		  AND name in (SELECT parent 
		                 FROM `tabDynamic Link` 
		                WHERE link_doctype = 'Customer' AND link_name = %(party)s
		                  AND parenttype = 'Address')""", {"party": party}, as_dict=True)


	address_data = {}
	if address:
		address_data = address[0]

	return address_data
		

def validate_filters(filters, account_details):

	if filters.from_date > filters.to_date:
		frappe.throw(_("From Date must be before To Date"))


def validate_party(filters):
	party_type, party = filters.get("party_type"), filters.get("party")

	if party:
		if not party_type:
			frappe.throw(_("To filter based on Party, select Party Type first"))
		else:
			for d in party:
				if not frappe.db.exists(party_type, d):
					frappe.throw(_("Invalid {0}: {1}").format(party_type, d))

def set_account_currency(filters):
	if filters.get("account") or (filters.get('party') and len(filters.party) == 1):
		filters["company_currency"] = frappe.get_cached_value('Company',  filters.company,  "default_currency")
		account_currency = None

		if filters.get("account"):
			account_currency = get_account_currency(filters.account)
		elif filters.get("party"):
			gle_currency = frappe.db.get_value(
				"GL Entry", {
					"party_type": filters.party_type, "party": filters.party[0], "company": filters.company
				},
				"account_currency"
			)

			if gle_currency:
				account_currency = gle_currency
			else:
				account_currency = (None if filters.party_type in ["Employee", "Student", "Shareholder", "Member"] else
					frappe.db.get_value(filters.party_type, filters.party[0], "default_currency"))

		filters["account_currency"] = account_currency or filters.company_currency
		if filters.account_currency != filters.company_currency and not filters.presentation_currency:
			filters.presentation_currency = filters.account_currency

	return filters

def get_result(filters, account_details):
	gl_entries = get_gl_entries(filters)

	data = get_data_with_opening_closing(filters, account_details, gl_entries)

	result = get_result_as_list(data, filters)

	return result

def get_gl_entries(filters):
	currency_map = get_currency(filters)
	select_fields = """, debit, credit, debit_in_account_currency,
		credit_in_account_currency """

	order_by_statement = "ORDER BY posting_date, account"

	if filters.get("group_by") == _("Group by Voucher"):
		order_by_statement = "ORDER BY posting_date, voucher_type, voucher_no"

	if filters.get("include_default_book_entries"):
		filters['company_fb'] = frappe.db.get_value("Company",
			filters.get("company"), 'default_finance_book')


	gl_entries = frappe.db.sql(
		"""
		SELECT
			posting_date, account, party_type, party,
			voucher_type, voucher_no, cost_center, project,
			against_voucher_type, against_voucher, account_currency,
			remarks, against, is_opening {select_fields}
		FROM `tabGL Entry`
		WHERE company=%(company)s {conditions}
		{order_by_statement}
		""".format(
			select_fields=select_fields, conditions=get_conditions(filters),
			order_by_statement=order_by_statement
		),
		filters, as_dict=1)

# 	gl_entries = update_payment_reference(gl_entries)

	if filters.get('presentation_currency'):
		return convert_to_presentation_currency(gl_entries, currency_map)
	else:
		return gl_entries


def get_conditions(filters):
	conditions = []
	if filters.get("account"):
		lft, rgt = frappe.db.get_value("Account", filters["account"], ["lft", "rgt"])
		conditions.append("""account IN (SELECT name FROM tabAccount
			WHERE lft>=%s and rgt<=%s and docstatus<2)""" % (lft, rgt))

	if filters.get("cost_center"):
		filters.cost_center = get_cost_centers_with_children(filters.cost_center)
		conditions.append("cost_center in %(cost_center)s")

	if filters.get("voucher_no"):
		conditions.append("voucher_no=%(voucher_no)s")

	if filters.get("party_type"):
		conditions.append("party_type=%(party_type)s")

	if filters.get("party"):
		conditions.append("party in %(party)s")

	if not (filters.get("account") or filters.get("party") or
		filters.get("group_by") in ["Group by Account", "Group by Party"]):
		conditions.append("posting_date >=%(from_date)s")

	conditions.append("(posting_date <=%(to_date)s or is_opening = 'Yes')")

	match_conditions = build_match_conditions("GL Entry")

	if match_conditions:
		conditions.append(match_conditions)

	accounting_dimensions = get_accounting_dimensions()

	if accounting_dimensions:
		for dimension in accounting_dimensions:
			if filters.get(dimension):
				conditions.append("{0} in (%({0})s)".format(dimension))

	return "and {}".format(" and ".join(conditions)) if conditions else ""


def get_data_with_opening_closing(filters, account_details, gl_entries):
	data = []

	gle_map = initialize_gle_map(gl_entries, filters)

	totals, entries = get_accountwise_gle(filters, gl_entries, gle_map)

	# Opening for filtered account
	data.append(totals.opening)

	if filters.get("group_by") != _('Group by Voucher (Consolidated)'):
		for acc, acc_dict in iteritems(gle_map):
			# acc
			if acc_dict.entries:
				# opening
				data.append({})
				if filters.get("group_by") != _("Group by Voucher"):
					data.append(acc_dict.totals.opening)

				data += acc_dict.entries

				# totals
				data.append(acc_dict.totals.total)

				# closing
				if filters.get("group_by") != _("Group by Voucher"):
					data.append(acc_dict.totals.closing)
		data.append({})
	else:
		data += entries

	# totals
	data.append(totals.total)

	# closing
	data.append(totals.closing)

	return data

def get_totals_dict():
	def _get_debit_credit_dict(label):
		return _dict(
			account="'{0}'".format(label),
			debit=0.0,
			credit=0.0,
			debit_in_account_currency=0.0,
			credit_in_account_currency=0.0
		)
	return _dict(
		opening = _get_debit_credit_dict(_('Opening')),
		total = _get_debit_credit_dict(_('Total')),
		closing = _get_debit_credit_dict(_('Closing (Opening + Total)'))
	)

def group_by_field(group_by):
	if group_by == _('Group by Party'):
		return 'party'
	elif group_by in [_('Group by Voucher (Consolidated)'), _('Group by Account')]:
		return 'account'
	else:
		return 'voucher_no'

def initialize_gle_map(gl_entries, filters):
	gle_map = OrderedDict()
	group_by = group_by_field(filters.get('group_by'))

	for gle in gl_entries:
		gle_map.setdefault(gle.get(group_by), _dict(totals=get_totals_dict(), entries=[]))
	return gle_map


def get_accountwise_gle(filters, gl_entries, gle_map):
	totals = get_totals_dict()
	entries = []
	consolidated_gle = OrderedDict()
	group_by = group_by_field(filters.get('group_by'))

	def update_value_in_dict(data, key, gle):
		data[key].debit += flt(gle.debit)
		data[key].credit += flt(gle.credit)

		data[key].debit_in_account_currency += flt(gle.debit_in_account_currency)
		data[key].credit_in_account_currency += flt(gle.credit_in_account_currency)

	from_date, to_date = getdate(filters.from_date), getdate(filters.to_date)
	for gle in gl_entries:
		if (gle.posting_date < from_date or
			(cstr(gle.is_opening) == "Yes" and not filters.get("show_opening_entries"))):
			update_value_in_dict(gle_map[gle.get(group_by)].totals, 'opening', gle)
			update_value_in_dict(totals, 'opening', gle)

			update_value_in_dict(gle_map[gle.get(group_by)].totals, 'closing', gle)
			update_value_in_dict(totals, 'closing', gle)

		elif gle.posting_date <= to_date:
			update_value_in_dict(gle_map[gle.get(group_by)].totals, 'total', gle)
			update_value_in_dict(totals, 'total', gle)
			if filters.get("group_by") != _('Group by Voucher (Consolidated)'):
				gle_map[gle.get(group_by)].entries.append(gle)
			elif filters.get("group_by") == _('Group by Voucher (Consolidated)'):
				key = (gle.get("voucher_type"), gle.get("voucher_no"),
					gle.get("account"), gle.get("cost_center"))
				if key not in consolidated_gle:
					consolidated_gle.setdefault(key, gle)
				else:
					update_value_in_dict(consolidated_gle, key, gle)

			update_value_in_dict(gle_map[gle.get(group_by)].totals, 'closing', gle)
			update_value_in_dict(totals, 'closing', gle)

	for key, value in consolidated_gle.items():
		entries.append(value)

	return totals, entries

def get_result_as_list(data, filters):
	balance, balance_in_account_currency = 0, 0
	inv_details = get_supplier_invoice_details()

	for d in data:
		if not d.get('posting_date'):
			balance, balance_in_account_currency = 0, 0

		balance = get_balance(d, balance, 'debit', 'credit')
		d['balance'] = balance

		d['account_currency'] = filters.account_currency
		d['bill_no'] = inv_details.get(d.get('against_voucher'), '')

	return data

def get_supplier_invoice_details():
	inv_details = {}
	for d in frappe.db.sql(""" select name, bill_no from `tabPurchase Invoice`
		where docstatus = 1 and bill_no is not null and bill_no != '' """, as_dict=1):
		inv_details[d.name] = d.bill_no

	return inv_details

def get_balance(row, balance, debit_field, credit_field):
	balance += (row.get(debit_field, 0) -  row.get(credit_field, 0))

	return balance

def update_payment_reference(gl_entries):
	"""
	Take a list of GL Entries and change the 'debit' and 'credit' values to currencies
	in `currency_info`.
	:param gl_entries:
	:return:
	"""
	converted_gl_list = []

	for entry in gl_entries:
		
		voucher_no = entry.get("voucher_no")
		
		payment_entry =  frappe.db.sql("""SELECT
				name, posting_date, reference_no, clearance_date, party
			FROM 
				`tabPayment Entry`
			WHERE 
				docstatus=1 and name = %s 
			LIMIT 1 """, voucher_no)
		
		if payment_entry:
			entry['voucher_no'] = payment_entry[0][2]

		converted_gl_list.append(entry)

	return converted_gl_list

def get_columns(filters):
	if filters.get("presentation_currency"):
		currency = filters["presentation_currency"]
	else:
		if filters.get("company"):
			currency = get_company_currency(filters["company"])
		else:
			company = get_default_company()
			currency = get_company_currency(company)

	columns = [
		{
			"label": _("Date"),
			"fieldname": "posting_date",
			"fieldtype": "Date",
			"width": 90
		},
 		{
 			"label": _("Description"),
 			"fieldname": "voucher_type",
 			"width": 120
 		},
 		{
 			"label": _("Reference"),
 			"fieldname": "voucher_no",
 			"fieldtype": "Dynamic Link",
 			"options": "voucher_type",
 			"width": 180
 		},
		{
			"label": _("Debit ({0})".format(currency)),
			"fieldname": "debit",
			"fieldtype": "Float",
			"width": 100
		},
		{
			"label": _("Credit ({0})".format(currency)),
			"fieldname": "credit",
			"fieldtype": "Float",
			"width": 100
		},
		{
			"label": _("Balance ({0})".format(currency)),
			"fieldname": "balance",
			"fieldtype": "Float",
			"width": 130
		}
	]

	columns.extend([
	])

	return columns

def ageing_bucket_totals(entries):

	current_total, days30_total, days60_total, days90_total = 0, 0, 0, 0  
	now  = date.today()
	
	for index in range(len(entries)):
		
		then = entries[index].get('posting_date')

		if not (then is None):
			
			duration = now - then
			days  = duration.days

			if days <= 30:
				current_total = current_total + entries[index].get('debit') - entries[index].get('credit') 
			
			if 60 <= days >= 31:
				days30_total = days30_total + entries[index].get('debit') - entries[index].get('credit')

			if 90 <= days >= 61:
				days60_total = days60_total + entries[index].get('debit') - entries[index].get('credit') 
			
			if days >= 90:
				days90_total = days90_total + entries[index].get('debit') - entries[index].get('credit') 
	
	
	return current_total, days30_total, days60_total, days90_total
