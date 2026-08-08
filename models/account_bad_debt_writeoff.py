# -*- coding: utf-8 -*-

from collections import defaultdict

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_is_zero


class AccountMove(models.Model):
    _inherit = "account.move"

    bad_debt_writeoff_move_id = fields.Many2one(
        "account.move",
        string="Bad Debt Write-Off Entry",
        copy=False,
        readonly=True,
        check_company=True,
        groups="account.group_account_user,base.group_system",
    )

    def _check_bad_debt_writeoff_access(self):
        if not (
            self.env.user.has_group("account.group_account_user")
            or self.env.user.has_group("base.group_system")
        ):
            raise AccessError(_("Only accountants and administrators may write off invoices."))

    def _check_bad_debt_writeoff_eligibility(self):
        self.ensure_one()
        self._check_bad_debt_writeoff_access()
        if self.move_type != "out_invoice":
            raise UserError(_("Only customer invoices can be written off with this action."))
        if self.state != "posted":
            raise UserError(_("The invoice must be posted before it can be written off."))
        if self.currency_id.is_zero(self.amount_residual):
            raise UserError(_("This invoice has no remaining balance to write off."))

    def action_open_bad_debt_writeoff(self):
        self.ensure_one()
        self._check_bad_debt_writeoff_eligibility()
        return {
            "type": "ir.actions.act_window",
            "name": _("Write Off Invoice"),
            "res_model": "account.move.bad.debt.writeoff.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_move_id": self.id,
            },
        }

    def action_view_bad_debt_writeoff_entry(self):
        self.ensure_one()
        self._check_bad_debt_writeoff_access()
        if not self.bad_debt_writeoff_move_id:
            raise UserError(_("No bad debt write-off entry is linked to this invoice."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Bad Debt Write-Off Entry"),
            "res_model": "account.move",
            "res_id": self.bad_debt_writeoff_move_id.id,
            "view_mode": "form",
            "target": "current",
        }


class AccountMoveBadDebtWriteoffWizard(models.TransientModel):
    _name = "account.move.bad.debt.writeoff.wizard"
    _description = "Customer Invoice Bad Debt Write-Off"

    move_id = fields.Many2one(
        "account.move",
        string="Invoice",
        required=True,
        readonly=True,
        check_company=True,
    )
    company_id = fields.Many2one(
        related="move_id.company_id",
        readonly=True,
    )
    currency_id = fields.Many2one(
        related="move_id.currency_id",
        readonly=True,
    )
    amount = fields.Monetary(
        string="Amount to Write Off",
        related="move_id.amount_residual",
        currency_field="currency_id",
        readonly=True,
    )
    date = fields.Date(
        string="Accounting Date",
        required=True,
        default=fields.Date.context_today,
    )
    journal_id = fields.Many2one(
        "account.journal",
        string="Journal",
        required=True,
        check_company=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]",
    )
    expense_account_id = fields.Many2one(
        "account.account",
        string="Bad Debt Expense Account",
        required=True,
        check_company=True,
        domain="[('deprecated', '=', False), ('account_type', 'not in', ('asset_receivable', 'liability_payable'))]",
    )
    reason = fields.Text(
        string="Reason",
        required=True,
        help="Document why the balance is considered uncollectible.",
    )

    @api.model
    def default_get(self, field_list):
        values = super().default_get(field_list)
        move = self.env["account.move"].browse(
            values.get("move_id") or self.env.context.get("active_id")
        ).exists()
        if not move:
            return values

        company = move.company_id
        if "journal_id" in field_list and not values.get("journal_id"):
            journal = self.env["account.journal"].search(
                [
                    ("company_id", "=", company.id),
                    ("type", "=", "general"),
                    ("code", "=", "VARIO"),
                ],
                limit=1,
            ) or self.env["account.journal"].search(
                [("company_id", "=", company.id), ("type", "=", "general")],
                limit=1,
            )
            values["journal_id"] = journal.id

        if "expense_account_id" in field_list and not values.get("expense_account_id"):
            account_model = self.env["account.account"]
            company_field = "company_ids" if "company_ids" in account_model._fields else "company_id"
            expense_account = account_model.search(
                [
                    (
                        company_field,
                        "in" if company_field == "company_ids" else "=",
                        [company.id] if company_field == "company_ids" else company.id,
                    ),
                    ("code", "=", "410801"),
                    ("deprecated", "=", False),
                ],
                limit=1,
            )
            values["expense_account_id"] = expense_account.id
        return values

    def _validate_configuration(self):
        self.ensure_one()
        invoice = self.move_id
        invoice._check_bad_debt_writeoff_eligibility()
        if self.journal_id.type != "general" or self.journal_id.company_id != invoice.company_id:
            raise ValidationError(_("Select a miscellaneous journal belonging to the invoice company."))
        if self.expense_account_id.deprecated:
            raise ValidationError(_("The selected bad debt account is deprecated."))
        if self.expense_account_id.account_type in ("asset_receivable", "liability_payable"):
            raise ValidationError(_("Select an expense account rather than a receivable or payable account."))
        account_companies = (
            self.expense_account_id.company_ids
            if "company_ids" in self.expense_account_id._fields
            else self.expense_account_id.company_id
        )
        if invoice.company_id not in account_companies:
            raise ValidationError(_("The selected bad debt account does not belong to the invoice company."))
        if not (self.reason or "").strip():
            raise ValidationError(_("Enter the reason why this balance is being written off."))

    def action_confirm(self):
        self.ensure_one()
        invoice = self.move_id
        invoice._check_bad_debt_writeoff_access()

        # Prevent two users from writing off the same residual balance concurrently.
        self.env.cr.execute("SELECT id FROM account_move WHERE id = %s FOR UPDATE", [invoice.id])
        invoice.invalidate_recordset(["amount_residual", "payment_state", "line_ids"])
        self._validate_configuration()
        invoice_currency_amount = invoice.amount_residual

        company_currency = invoice.company_currency_id
        receivable_lines = invoice.line_ids.filtered(
            lambda line: line.account_id.account_type == "asset_receivable"
            and not line.reconciled
            and not float_is_zero(
                line.amount_residual,
                precision_rounding=company_currency.rounding,
            )
        )
        if not receivable_lines:
            raise UserError(_("No open receivable lines remain on this invoice."))
        if any(line.amount_residual <= 0 for line in receivable_lines):
            raise UserError(_("The remaining receivable balance cannot be written off automatically."))

        grouped_lines = defaultdict(lambda: self.env["account.move.line"])
        for line in receivable_lines:
            grouped_lines[(line.account_id, line.currency_id)] |= line

        writeoff_label = _("Bad debt write-off - %s") % invoice.name
        entry_line_values = []
        group_sequences = {}
        sequence = 10
        for (receivable_account, currency), source_lines in grouped_lines.items():
            company_amount = sum(source_lines.mapped("amount_residual"))
            if float_is_zero(company_amount, precision_rounding=company_currency.rounding):
                continue

            entry_line_values.append((0, 0, {
                "name": writeoff_label,
                "sequence": sequence,
                "account_id": self.expense_account_id.id,
                "partner_id": invoice.commercial_partner_id.id,
                "debit": company_amount,
                "credit": 0.0,
            }))
            counterpart_sequence = sequence + 1
            counterpart_values = {
                "name": writeoff_label,
                "sequence": counterpart_sequence,
                "account_id": receivable_account.id,
                "partner_id": invoice.commercial_partner_id.id,
                "debit": 0.0,
                "credit": company_amount,
            }
            if currency and currency != company_currency:
                counterpart_values.update({
                    "currency_id": currency.id,
                    "amount_currency": -sum(source_lines.mapped("amount_residual_currency")),
                })
            entry_line_values.append((0, 0, counterpart_values))
            group_sequences[(receivable_account, currency)] = counterpart_sequence
            sequence += 10

        if not entry_line_values:
            raise UserError(_("No balance remains to write off."))

        writeoff_move = self.env["account.move"].create({
            "move_type": "entry",
            "company_id": invoice.company_id.id,
            "journal_id": self.journal_id.id,
            "date": self.date,
            "ref": _("Bad debt write-off for %s") % invoice.name,
            "line_ids": entry_line_values,
        })
        writeoff_move.action_post()

        for key, source_lines in grouped_lines.items():
            counterpart = writeoff_move.line_ids.filtered(
                lambda line: line.sequence == group_sequences.get(key)
            )
            if len(counterpart) != 1:
                raise UserError(_("The write-off entry could not be reconciled automatically."))
            (source_lines | counterpart).reconcile()

        invoice.bad_debt_writeoff_move_id = writeoff_move
        invoice.message_post(
            body=Markup(
                "<p><strong>%s</strong></p>"
                "<p>%s: %s<br/>%s: %s<br/>%s: %s<br/>%s: %s</p>"
            ) % (
                escape(_("Invoice balance written off as bad debt")),
                escape(_("Write-off entry")),
                escape(writeoff_move.display_name),
                escape(_("Amount")),
                escape("%.2f %s" % (invoice_currency_amount, invoice.currency_id.name)),
                escape(_("Accounting date")),
                escape(fields.Date.to_string(self.date)),
                escape(_("Reason")),
                escape(self.reason.strip()),
            ),
            subtype_xmlid="mail.mt_note",
        )
        writeoff_move.message_post(
            body=Markup("<p>%s: %s</p><p>%s: %s</p>") % (
                escape(_("Created from customer invoice")),
                escape(invoice.display_name),
                escape(_("Reason")),
                escape(self.reason.strip()),
            ),
            subtype_xmlid="mail.mt_note",
        )

        return {
            "type": "ir.actions.act_window",
            "name": _("Bad Debt Write-Off Entry"),
            "res_model": "account.move",
            "res_id": writeoff_move.id,
            "view_mode": "form",
            "target": "current",
        }
