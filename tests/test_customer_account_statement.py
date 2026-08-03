# -*- coding: utf-8 -*-
from datetime import date

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestCustomerAccountStatement(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.customer = cls.env["res.partner"].create({
            "name": "Account Statement Customer",
            "customer_rank": 1,
        })
        cls.billing_contact = cls.env["res.partner"].create({
            "name": "Billing Contact",
            "parent_id": cls.customer.id,
            "type": "invoice",
        })
        cls.receivable_account = cls.customer.property_account_receivable_id
        cls.counterpart_account = cls.env["account.account"].search([
            ("company_id", "=", cls.env.company.id),
            ("account_type", "=", "income"),
            ("deprecated", "=", False),
        ], limit=1)
        cls.journal = cls.env["account.journal"].search([
            ("company_id", "=", cls.env.company.id),
            ("type", "=", "general"),
        ], limit=1)

        cls._post_receivable_entry(date(2026, 1, 15), 100.0, "Opening invoice")
        cls._post_receivable_entry(
            date(2026, 2, 5),
            50.0,
            "Period invoice",
            partner=cls.billing_contact,
        )
        cls._post_receivable_entry(date(2026, 2, 12), -25.0, "Period payment")
        cls._post_receivable_entry(date(2026, 3, 1), 40.0, "Future invoice")

    @classmethod
    def _post_receivable_entry(cls, entry_date, amount, name, partner=None):
        partner = partner or cls.customer
        receivable_values = {
            "name": name,
            "partner_id": partner.id,
            "account_id": cls.receivable_account.id,
            "debit": max(amount, 0.0),
            "credit": max(-amount, 0.0),
        }
        counterpart_values = {
            "name": name,
            "account_id": cls.counterpart_account.id,
            "debit": max(-amount, 0.0),
            "credit": max(amount, 0.0),
        }
        move = cls.env["account.move"].create({
            "move_type": "entry",
            "date": entry_date,
            "journal_id": cls.journal.id,
            "ref": name,
            "line_ids": [(0, 0, receivable_values), (0, 0, counterpart_values)],
        })
        move.action_post()
        return move

    def _wizard(self):
        return self.env["customer.account.statement.wizard"].create({
            "partner_id": self.customer.id,
            "company_id": self.env.company.id,
            "date_from": date(2026, 2, 1),
            "date_to": date(2026, 2, 28),
        })

    def test_statement_balances_and_includes_child_contact(self):
        data = self._wizard()._get_statement_data()

        self.assertEqual(data["opening_balance"], 100.0)
        self.assertEqual(data["period_debit"], 50.0)
        self.assertEqual(data["period_credit"], 25.0)
        self.assertEqual(data["closing_balance"], 125.0)
        self.assertEqual(len(data["entries"]), 2)
        self.assertEqual(data["entries"][0]["balance"], 150.0)
        self.assertEqual(data["entries"][1]["balance"], 125.0)

    def test_print_uses_customer_statement_report(self):
        action = self._wizard().action_print()

        self.assertEqual(
            action["report_name"],
            "contract_management.report_customer_account_statement_document",
        )

    def test_print_uses_customer_language(self):
        self.customer.lang = "es_419"

        action = self._wizard().action_print()

        self.assertEqual(action["context"]["lang"], "es_419")

    def test_statement_report_bypasses_direct_print(self):
        report = self.env.ref(
            "contract_management.action_report_customer_account_statement"
        )
        policy = self.env["printnode.report.policy"].search([
            ("report_id", "=", report.id),
        ])

        self.assertEqual(len(policy), 1)
        self.assertTrue(policy.exclude_from_auto_printing)
        self.assertEqual(policy.report_id, report)

    def test_rejects_reversed_date_range(self):
        with self.assertRaises(ValidationError):
            self.env["customer.account.statement.wizard"].create({
                "partner_id": self.customer.id,
                "date_from": date(2026, 2, 28),
                "date_to": date(2026, 2, 1),
            })
