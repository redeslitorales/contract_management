# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.tools.misc import format_amount, format_date
from markupsafe import Markup, escape


class CustomerAccountStatementWizard(models.TransientModel):
    _name = "customer.account.statement.wizard"
    _description = "Customer Account Statement"

    partner_id = fields.Many2one(
        "res.partner",
        string="Customer",
        required=True,
        domain="[('customer_rank', '>', 0)]",
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        required=True,
        default=lambda self: self.env.company,
    )
    currency_id = fields.Many2one(
        related="company_id.currency_id",
        readonly=True,
    )
    date_from = fields.Date(
        string="From",
        required=True,
        default=lambda self: fields.Date.start_of(
            fields.Date.context_today(self),
            "month",
        ),
    )
    date_to = fields.Date(
        string="Through",
        required=True,
        default=lambda self: fields.Date.context_today(self),
    )

    @api.constrains("date_from", "date_to")
    def _check_dates(self):
        for wizard in self:
            if (
                wizard.date_from
                and wizard.date_to
                and wizard.date_from > wizard.date_to
            ):
                raise ValidationError(
                    _("From must be on or before Through.")
                )

    @api.onchange("partner_id")
    def _onchange_partner_id(self):
        if self.partner_id:
            self.partner_id = self.partner_id.commercial_partner_id

    def _receivable_line_domain(self):
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id
        return [
            ("company_id", "=", self.company_id.id),
            ("partner_id", "child_of", partner.id),
            ("parent_state", "=", "posted"),
            ("account_id.account_type", "=", "asset_receivable"),
        ]

    def _entry_label(self, line):
        move = line.move_id
        labels = {
            "out_invoice": _("Invoice"),
            "out_refund": _("Credit Note"),
            "out_receipt": _("Sales Receipt"),
        }
        if move.move_type in labels:
            return labels[move.move_type]
        if line.payment_id or line.statement_line_id:
            return _("Payment")
        return _("Journal Entry")

    def _entry_reference(self, line):
        move = line.move_id
        references = [move.name]
        supplemental = getattr(move, "payment_reference", False) or move.ref
        if supplemental and supplemental not in references:
            references.append(supplemental)
        return " - ".join(filter(None, references)) or line.name or _("Entry")

    def _get_statement_data(self):
        """Return the posted receivable ledger for the selected cutoff dates."""
        self.ensure_one()
        domain = self._receivable_line_domain()
        opening_lines = self.env["account.move.line"].search(
            domain + [("date", "<", self.date_from)]
        )
        period_lines = self.env["account.move.line"].search(
            domain
            + [
                ("date", ">=", self.date_from),
                ("date", "<=", self.date_to),
            ],
            order="date, id",
        )

        opening_balance = sum(opening_lines.mapped("balance"))
        running_balance = opening_balance
        entries = []
        for line in period_lines:
            running_balance += line.balance
            entries.append({
                "date": line.date,
                "date_maturity": line.date_maturity,
                "entry_type": self._entry_label(line),
                "reference": self._entry_reference(line),
                "debit": line.debit,
                "credit": line.credit,
                "balance": running_balance,
            })

        period_debit = sum(period_lines.mapped("debit"))
        period_credit = sum(period_lines.mapped("credit"))
        return {
            "opening_balance": opening_balance,
            "period_debit": period_debit,
            "period_credit": period_credit,
            "closing_balance": opening_balance + period_debit - period_credit,
            "entries": entries,
            "issued_date": fields.Date.context_today(self),
            "reference": "AS-%s-%s" % (
                self.partner_id.commercial_partner_id.id,
                self.date_to.strftime("%Y%m%d"),
            ),
        }

    def _format_amount(self, amount):
        self.ensure_one()
        return format_amount(
            self.env,
            amount,
            self.currency_id,
        ).replace("\u00a0", " ").replace("\u202f", " ").replace("\ufeff", "")

    def _pdf_text(self, value):
        """Render Unicode safely through wkhtmltopdf's legacy HTML decoder.

        The production wkhtmltopdf build interprets literal UTF-8 bytes as
        Latin-1.  ASCII numeric entities survive that boundary and are decoded
        back to the intended Unicode characters by the HTML parser.
        """
        self.ensure_one()
        escaped = str(escape(value or ""))
        return Markup(escaped.encode("ascii", "xmlcharrefreplace").decode("ascii"))

    def _company_address_lines(self):
        self.ensure_one()
        partner = self.company_id.partner_id
        address = partner._display_address(without_company=True)
        return [
            line.strip()
            for line in ([self.company_id.name] + address.splitlines())
            if line and line.strip()
        ]

    def _format_date(self, value):
        self.ensure_one()
        return format_date(self.env, value) if value else ""

    def _get_report_labels(self):
        self.ensure_one()
        return {
            "kicker": _("Customer accounts"),
            "title": _("Account Statement"),
            "reference": _("Reference"),
            "customer": _("Customer"),
            "tax_id": _("Tax ID"),
            "statement_period": _("Statement period"),
            "issued": _("Issued"),
            "opening_balance": _("Opening balance"),
            "charges": _("Charges"),
            "payments_credits": _("Payments / credits"),
            "closing_balance": _("Closing balance"),
            "account_activity": _("Account activity"),
            "date": _("Date"),
            "due": _("Due"),
            "type": _("Type"),
            "credits": _("Credits"),
            "balance": _("Balance"),
            "no_activity": _(
                "No posted account activity was found in this period."
            ),
            "period_totals": _("Period totals"),
            "note_prefix": _(
                "This statement reflects posted accounting entries through"
            ),
            "note_suffix": _(
                "Please contact us if you have questions about any transaction."
            ),
            "generated_by": _("Generated electronically by"),
            "amounts_in": _("Amounts are shown in"),
        }

    def action_print(self):
        self.ensure_one()
        language = self.partner_id.lang or self.env.user.lang
        statement = self.with_context(lang=language)
        return self.env.ref(
            "contract_management.action_report_customer_account_statement"
        ).with_context(lang=language).report_action(statement)


class PrintNodeReportPolicy(models.Model):
    _inherit = "printnode.report.policy"

    @api.model
    def configure_customer_account_statement_policy(self):
        report = self.env.ref(
            "contract_management.action_report_customer_account_statement"
        )
        policy = self.search([("report_id", "=", report.id)], limit=1)
        if policy:
            policy.write({"exclude_from_auto_printing": True})
        else:
            policy = self.create({
                "report_id": report.id,
                "exclude_from_auto_printing": True,
            })
        return policy


class CustomerAccountStatementReportEncoding(models.Model):
    _inherit = "ir.actions.report"

    @api.model
    def _build_wkhtmltopdf_args(
        self,
        paperformat_id,
        landscape,
        specific_paperformat_args=None,
        set_viewport_size=False,
    ):
        command_args = super()._build_wkhtmltopdf_args(
            paperformat_id,
            landscape,
            specific_paperformat_args=specific_paperformat_args,
            set_viewport_size=set_viewport_size,
        )
        statement_format = self.env.ref(
            "contract_management.customer_account_statement_paperformat",
            raise_if_not_found=False,
        )
        if statement_format and paperformat_id == statement_format:
            command_args.extend(["--encoding", "utf-8"])
        return command_args
