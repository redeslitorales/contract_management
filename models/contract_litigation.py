# -*- coding: utf-8 -*-

import base64
import io
import json
import logging
import re
import zipfile
from datetime import time, timedelta
from urllib.parse import quote

import requests
from PyPDF2 import PdfFileReader, PdfFileWriter
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_compare
from odoo.tools.misc import format_amount, format_date


LITIGATION_CASE_STATES = [
    ("review", "Review"),
    ("remediation", "Remediation"),
    ("ready", "Ready for Notice"),
    ("initial_notice", "Initial Notice Sent"),
    ("final_demand", "Final Demand Sent"),
    ("counsel", "Referred to Counsel"),
    ("filed", "Filed"),
    ("settled", "Settled"),
    ("closed", "Closed"),
    ("cancelled", "Cancelled"),
]

LITIGATION_WHATSAPP_CONTRACT_TEMPLATE = "litigation_initial_notice_contract_v2"
LITIGATION_WHATSAPP_BALANCE_TEMPLATE = "litigation_initial_notice_balance"
LITIGATION_LANGUAGE = "es_419"
LITIGATION_EMAIL_FROM = "Cabal <DoNotReply@cabal.sv>"
LITIGATION_EMAIL_REPLY_TO = "legal@cabal.sv"
LITIGATION_AZURE_SMTP_HOST = "smtp.azurecomm.net"
LITIGATION_AZURE_SMTP_USER = "notifications@cabal.sv"
_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    litigation_suspended_on = fields.Date(
        string="Litigation Suspension Date",
        copy=False,
        tracking=True,
        help=(
            "Date used to age a suspended subscription for pre-litigation review. "
            "It is synchronized from the network suspension date when available."
        ),
        groups="contract_management.group_contract_litigation_user",
    )
    litigation_suspension_date_source = fields.Selection(
        [
            ("network", "Network Suspension"),
            ("state_change", "Subscription State Change"),
            ("estimated", "Estimated from Last Update"),
            ("manual", "Manual"),
        ],
        string="Suspension Date Source",
        copy=False,
        readonly=True,
        groups="contract_management.group_contract_litigation_user",
    )
    litigation_hold = fields.Boolean(
        string="Litigation Hold",
        copy=False,
        tracking=True,
        help="Prevents automatic case creation and legal escalation for this subscription.",
        groups="contract_management.group_contract_litigation_user",
    )
    litigation_hold_reason = fields.Text(
        string="Litigation Hold Reason",
        copy=False,
        tracking=True,
        groups="contract_management.group_contract_litigation_user",
    )
    litigation_case_ids = fields.One2many(
        "contract.litigation.case", "subscription_id", string="Litigation Cases", copy=False
    )
    litigation_case_count = fields.Integer(compute="_compute_litigation_case_count")

    @api.constrains("litigation_hold", "litigation_hold_reason")
    def _check_litigation_hold_reason(self):
        for order in self:
            if order.litigation_hold and not (order.litigation_hold_reason or "").strip():
                raise ValidationError(_("A reason is required when placing a subscription on litigation hold."))

    @api.depends("litigation_case_ids")
    def _compute_litigation_case_count(self):
        for order in self:
            order.litigation_case_count = len(order.litigation_case_ids)

    def _litigation_sync_suspension_date(self):
        """Backfill conservatively without requiring the optional network addon."""
        today = fields.Date.context_today(self)
        for order in self.filtered(lambda item: item.subscription_state == "8_suspend"):
            if order.litigation_suspended_on:
                continue
            network_date = False
            if "suspension_effective_date" in order._fields:
                network_date = order["suspension_effective_date"]
            if network_date:
                order.with_context(litigation_date_sync=True).write({
                    "litigation_suspended_on": fields.Date.to_date(network_date),
                    "litigation_suspension_date_source": "network",
                })
            else:
                # write_date can only make an old suspension look newer, so it is a
                # conservative fallback and will not prematurely qualify an account.
                estimated = fields.Date.to_date(order.write_date) if order.write_date else today
                order.with_context(litigation_date_sync=True).write({
                    "litigation_suspended_on": estimated,
                    "litigation_suspension_date_source": "estimated",
                })
        return True

    def write(self, vals):
        previous_states = {order.id: order.subscription_state for order in self}
        if (
            not self.env.context.get("litigation_date_sync")
            and "litigation_suspended_on" in vals
            and "litigation_suspension_date_source" not in vals
        ):
            vals["litigation_suspension_date_source"] = "manual"
        result = super().write(vals)
        if self.env.context.get("litigation_date_sync"):
            return result

        for order in self:
            entering_suspension = (
                vals.get("subscription_state") == "8_suspend"
                and previous_states.get(order.id) != "8_suspend"
            )
            if not entering_suspension:
                continue
            network_date = False
            if "suspension_effective_date" in order._fields:
                network_date = vals.get("suspension_effective_date") or order["suspension_effective_date"]
            manual_date = vals.get("litigation_suspended_on")
            order.sudo().with_context(litigation_date_sync=True).write({
                "litigation_suspended_on": fields.Date.to_date(network_date or manual_date)
                if (network_date or manual_date)
                else fields.Date.context_today(order),
                "litigation_suspension_date_source": (
                    "network" if network_date else "manual" if manual_date else "state_change"
                ),
            })
        return result

    def action_view_litigation_cases(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id(
            "contract_management.action_contract_litigation_cases"
        )
        action["domain"] = [("subscription_id", "=", self.id)]
        action["context"] = {"default_subscription_id": self.id}
        return action


class ContractLitigationCase(models.Model):
    _name = "contract.litigation.case"
    _description = "Subscription Litigation Case"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "suspended_on desc, id desc"
    _check_company_auto = True

    name = fields.Char(default=lambda self: _("New"), readonly=True, copy=False, tracking=True)
    active = fields.Boolean(default=True)
    state = fields.Selection(
        LITIGATION_CASE_STATES,
        default="review",
        required=True,
        tracking=True,
        index=True,
    )
    subscription_id = fields.Many2one(
        "sale.order",
        string="Subscription",
        required=True,
        ondelete="restrict",
        check_company=True,
        tracking=True,
        domain="[('is_subscription', '=', True)]",
    )
    partner_id = fields.Many2one(
        related="subscription_id.partner_id", string="Customer", store=True, readonly=True
    )
    company_id = fields.Many2one(
        related="subscription_id.company_id", store=True, readonly=True
    )
    currency_id = fields.Many2one(
        related="company_id.currency_id", store=True, readonly=True
    )
    responsible_id = fields.Many2one(
        "res.users",
        string="Case Owner",
        default=lambda self: self.env.user,
        tracking=True,
        domain=[("share", "=", False), ("active", "=", True)],
    )
    suspended_on = fields.Date(required=True, tracking=True)
    suspension_date_source = fields.Selection(
        related="subscription_id.litigation_suspension_date_source", readonly=True
    )
    threshold_days = fields.Integer(default=90, required=True, readonly=True)
    days_suspended = fields.Integer(compute="_compute_days_suspended")
    notice_deadline = fields.Date(copy=False, tracking=True)
    referred_on = fields.Date(copy=False, tracking=True)
    filed_on = fields.Date(copy=False, tracking=True)
    settled_on = fields.Date(copy=False, tracking=True)
    settlement_amount = fields.Monetary(currency_field="currency_id", copy=False, tracking=True)
    court_reference = fields.Char(copy=False, tracking=True)
    contact_email = fields.Char(copy=False)
    contact_phone = fields.Char(copy=False)
    contact_whatsapp = fields.Char(
        string="WhatsApp",
        copy=False,
        help="Verified WhatsApp number used for litigation notices.",
    )

    invoice_line_ids = fields.One2many(
        "contract.litigation.case.invoice", "case_id", string="Invoice Snapshot", copy=False
    )
    communication_ids = fields.One2many(
        "contract.litigation.communication", "case_id", string="Communications", copy=False
    )
    gross_open_balance = fields.Monetary(
        compute="_compute_balances", store=True, currency_field="currency_id"
    )
    claim_balance = fields.Monetary(
        compute="_compute_balances", store=True, currency_field="currency_id", tracking=True
    )
    post_suspension_balance = fields.Monetary(
        compute="_compute_balances", store=True, currency_field="currency_id"
    )
    current_open_balance = fields.Monetary(
        compute="_compute_current_open_balance", currency_field="currency_id"
    )
    balance_changed = fields.Boolean(compute="_compute_current_open_balance")
    snapshot_date = fields.Date(readonly=True, copy=False)
    package_attachment_id = fields.Many2one(
        "ir.attachment", string="Latest Package", readonly=True, copy=False
    )
    package_generated_on = fields.Datetime(readonly=True, copy=False)

    legal_hold = fields.Boolean(related="subscription_id.litigation_hold", readonly=True)
    legal_hold_reason = fields.Text(related="subscription_id.litigation_hold_reason", readonly=True)
    has_contract = fields.Boolean(compute="_compute_contract_evidence", store=True)
    has_contract_evidence = fields.Boolean(compute="_compute_contract_evidence", store=True)
    applicable_contract_id = fields.Many2one(
        "contract.management",
        compute="_compute_contract_evidence",
        string="Applicable Contract",
        store=True,
        index=True,
        readonly=True,
    )
    pagare_face_value = fields.Monetary(
        compute="_compute_pagare_face_value_compat",
        currency_field="currency_id",
        string="Legacy Pagaré Amount (Not Used)",
        help=(
            "Deprecated compatibility field for previously cached views. It always returns zero; "
            "litigation notices and calculations do not use the pagaré nominal amount."
        ),
    )
    pagare_adjusted_amount = fields.Monetary(
        compute="_compute_pagare_adjusted_amount",
        currency_field="currency_id",
        string="Adjusted Pagaré Amount",
        help=(
            "Current early-termination amount after applying paid subscription invoices and "
            "the contractual early-termination adjustment. The nominal face value is not shown."
        ),
    )
    pagare_verified = fields.Boolean(
        string="Signed Pagaré Verified",
        tracking=True,
        help=(
            "Required when a contract exists. Confirm that the signed contract, or the latest "
            "signed replacement addendum, contains the customer-signed pagaré before escalation."
        ),
    )
    has_post_suspension_items = fields.Boolean(compute="_compute_balances", store=True)
    readiness_status = fields.Selection(
        [
            ("blocked", "Blocked"),
            ("remediation", "Needs Remediation"),
            ("ready", "Ready"),
        ],
        compute="_compute_readiness",
        store=True,
    )
    # Readiness explanations contain translated sentences.  They must be
    # computed in the requesting user's language instead of being persisted in
    # whichever language happened to run the last stored recomputation.
    readiness_notes = fields.Text(compute="_compute_readiness")

    identity_verified = fields.Boolean(tracking=True)
    contract_reviewed = fields.Boolean(tracking=True)
    service_delivery_verified = fields.Boolean(tracking=True)
    balance_reviewed = fields.Boolean(tracking=True)
    post_suspension_reviewed = fields.Boolean(tracking=True)
    disputes_cleared = fields.Boolean(tracking=True)
    address_verified = fields.Boolean(tracking=True)
    manager_approved = fields.Boolean(tracking=True)
    reviewer_notes = fields.Html()
    counsel_notes = fields.Html(groups="contract_management.group_contract_litigation_manager")
    closure_reason = fields.Selection(
        [
            ("paid", "Paid"),
            ("settled", "Settlement"),
            ("written_off", "Written Off"),
            ("insufficient_evidence", "Insufficient Evidence"),
            ("not_economic", "Not Economical"),
            ("other", "Other"),
        ],
        copy=False,
        tracking=True,
    )

    _sql_constraints = [
        (
            "subscription_suspension_uniq",
            "unique(subscription_id, suspended_on)",
            "A litigation case already exists for this subscription suspension date.",
        )
    ]

    @api.model_create_multi
    def create(self, vals_list):
        sequence = self.env["ir.sequence"]
        for vals in vals_list:
            vals["state"] = "review"
            if not vals.get("name") or vals.get("name") == _("New"):
                vals["name"] = sequence.next_by_code("contract.litigation.case") or _("New")
            subscription = self.env["sale.order"].browse(vals.get("subscription_id")).exists()
            if subscription:
                vals.setdefault("contact_email", subscription.partner_id.email)
                vals.setdefault("contact_phone", subscription.partner_id.phone or subscription.partner_id.mobile)
                vals.setdefault(
                    "contact_whatsapp",
                    (
                        subscription.partner_id.whatsapp
                        if "whatsapp" in subscription.partner_id._fields
                        else False
                    )
                    or subscription.partner_id.mobile
                    or subscription.partner_id.phone,
                )
                vals.setdefault("suspended_on", subscription.litigation_suspended_on)
        cases = super().create(vals_list)
        cases.filtered(lambda case: not case.invoice_line_ids).action_refresh_snapshot()
        return cases

    def unlink(self):
        if any(case.state not in ("review", "remediation", "cancelled") for case in self):
            raise UserError(_("Only review, remediation, or cancelled cases may be deleted."))
        return super().unlink()

    def write(self, vals):
        manager_only = {
            "counsel_notes",
            "referred_on",
            "filed_on",
            "settled_on",
            "settlement_amount",
            "court_reference",
            "closure_reason",
        }
        manager_states = {"final_demand", "counsel", "filed", "settled", "closed"}
        is_manager = self.env.user.has_group("contract_management.group_contract_litigation_manager")
        if "state" in vals and not self.env.context.get("litigation_workflow_action"):
            raise UserError(_("Use the litigation workflow buttons to change the case stage."))
        if not is_manager and (
            manager_only.intersection(vals)
            or vals.get("manager_approved") is True
            or vals.get("state") in manager_states
        ):
            raise AccessError(_("Only a Litigation Manager may approve or legally escalate a case."))
        if any(case.state not in ("review", "remediation") for case in self):
            locked = {"subscription_id", "suspended_on", "threshold_days"}
            if locked.intersection(vals):
                raise UserError(_("Core case data is locked after the review stage."))
        return super().write(vals)

    @api.depends("suspended_on", "threshold_days")
    def _compute_days_suspended(self):
        today = fields.Date.context_today(self)
        for case in self:
            case.days_suspended = max((today - case.suspended_on).days, 0) if case.suspended_on else 0

    @api.depends(
        "invoice_line_ids.residual_amount",
        "invoice_line_ids.include_in_claim",
        "invoice_line_ids.post_suspension",
    )
    def _compute_balances(self):
        for case in self:
            case.gross_open_balance = sum(case.invoice_line_ids.mapped("residual_amount"))
            case.claim_balance = sum(
                case.invoice_line_ids.filtered("include_in_claim").mapped("residual_amount")
            )
            post_lines = case.invoice_line_ids.filtered("post_suspension")
            case.post_suspension_balance = sum(post_lines.mapped("residual_amount"))
            case.has_post_suspension_items = bool(post_lines)

    @api.depends(
        "subscription_id.invoice_ids.state",
        "subscription_id.invoice_ids.move_type",
        "subscription_id.invoice_ids.amount_residual",
        "gross_open_balance",
    )
    def _compute_current_open_balance(self):
        for case in self:
            current = case._get_current_open_balance()
            case.current_open_balance = current
            case.balance_changed = bool(
                case.snapshot_date
                and float_compare(
                    current,
                    case.gross_open_balance,
                    precision_rounding=case.currency_id.rounding,
                )
                != 0
            )

    def _get_current_open_balance(self):
        self.ensure_one()
        total = 0.0
        for move in self.subscription_id.invoice_ids.filtered(
            lambda item: item.state == "posted"
            and item.move_type in ("out_invoice", "out_refund", "out_receipt")
            and item.amount_residual
        ):
            total += (-1.0 if move.move_type == "out_refund" else 1.0) * move.amount_residual
        return total

    def _ensure_snapshot_current(self):
        self.ensure_one()
        if self.balance_changed:
            raise UserError(
                _(
                    "The live open balance has changed since the case snapshot. "
                    "Return the case to remediation and refresh the balance before continuing."
                )
            )

    @api.depends(
        "subscription_id.contract_ids",
        "subscription_id.contract_ids.state",
        "subscription_id.contract_ids.contract_file",
        "subscription_id.contract_ids.docusign_status",
        "subscription_id.contract_ids.docusign_id.connector_line_ids.signed_attachment_ids",
    )
    def _compute_contract_evidence(self):
        for case in self:
            contracts = case.subscription_id.contract_ids
            enforceable_contracts = contracts.filtered(
                lambda contract: contract.state
                in ("active", "renewal_due", "auto_renewed", "expired", "terminated")
            )
            evidence_contracts = enforceable_contracts.filtered(
                lambda contract: bool(contract.contract_file or contract.has_signed_documents)
            )
            applicable_pool = evidence_contracts or enforceable_contracts or contracts
            applicable_contract = applicable_pool.sorted(
                key=lambda contract: (contract.start_date or fields.Date.from_string("1900-01-01"), contract.id)
            )[-1:] if applicable_pool else applicable_pool
            # Keep the contract-record fact separate from whether it may be
            # relied upon as evidence in the litigation notice.
            case.has_contract = bool(contracts)
            case.applicable_contract_id = applicable_contract
            case.has_contract_evidence = bool(evidence_contracts)

    def _compute_pagare_face_value_compat(self):
        for case in self:
            case.pagare_face_value = 0.0

    @api.depends(
        "applicable_contract_id",
        "applicable_contract_id.contract_value",
        "applicable_contract_id.early_termination_fee",
        "applicable_contract_id.subscription_id.invoice_ids.state",
        "applicable_contract_id.subscription_id.invoice_ids.move_type",
        "applicable_contract_id.subscription_id.invoice_ids.payment_state",
        "applicable_contract_id.subscription_id.invoice_ids.amount_total",
        "applicable_contract_id.subscription_id.invoice_ids.invoice_line_ids.price_total",
        "applicable_contract_id.subscription_id.invoice_ids.invoice_line_ids.sale_line_ids.product_id.recurring_invoice",
    )
    def _compute_pagare_adjusted_amount(self):
        for case in self:
            case.pagare_adjusted_amount = max(
                case.applicable_contract_id.early_termination_cost or 0.0,
                0.0,
            ) if case.applicable_contract_id else 0.0

    @api.depends(
        "subscription_id.subscription_state",
        "subscription_id.litigation_hold",
        "suspended_on",
        "threshold_days",
        "claim_balance",
        "identity_verified",
        "contract_reviewed",
        "service_delivery_verified",
        "balance_reviewed",
        "post_suspension_reviewed",
        "disputes_cleared",
        "address_verified",
        "has_contract_evidence",
        "pagare_verified",
        "has_post_suspension_items",
        "subscription_id.invoice_ids.state",
        "subscription_id.invoice_ids.amount_residual",
    )
    def _compute_readiness(self):
        today = fields.Date.context_today(self)
        for case in self:
            blockers = []
            remediation = []
            if case.subscription_id.subscription_state != "8_suspend":
                blockers.append(_("subscription is no longer suspended"))
            if case.legal_hold:
                blockers.append(_("a litigation hold is active"))
            if not case.suspended_on or (today - case.suspended_on).days < case.threshold_days:
                blockers.append(_("the suspension-age threshold has not been reached"))
            if float_compare(case.claim_balance, 0.0, precision_rounding=case.currency_id.rounding) <= 0:
                blockers.append(_("there is no positive reviewed claim balance"))
            if case.balance_changed:
                blockers.append(_("the live balance has changed since the invoice snapshot"))

            checklist = [
                (case.identity_verified, _("customer identity is not verified")),
                (case.contract_reviewed, _("contract evidence is not reviewed")),
                (case.service_delivery_verified, _("service delivery is not verified")),
                (case.balance_reviewed, _("balance is not reviewed")),
                (case.disputes_cleared, _("disputes are not cleared")),
                (case.address_verified, _("notice address is not verified")),
            ]
            remediation.extend(message for complete, message in checklist if not complete)
            if case.has_contract_evidence and not case.pagare_verified:
                remediation.append(_("the customer-signed pagaré in the contract is not verified"))
            if case.has_post_suspension_items and not case.post_suspension_reviewed:
                remediation.append(_("post-suspension charges are not reviewed"))

            if blockers:
                case.readiness_status = "blocked"
                case.readiness_notes = "; ".join(blockers + remediation)
            elif remediation:
                case.readiness_status = "remediation"
                case.readiness_notes = "; ".join(remediation)
            else:
                case.readiness_status = "ready"
                case.readiness_notes = _("All required pre-litigation checks are complete.")

    @api.constrains("suspended_on")
    def _check_suspended_on(self):
        today = fields.Date.context_today(self)
        for case in self:
            if case.suspended_on and case.suspended_on > today:
                raise ValidationError(_("The suspension date cannot be in the future."))

    @api.constrains("responsible_id")
    def _check_responsible_internal_user(self):
        for case in self:
            if case.responsible_id and case.responsible_id.share:
                raise ValidationError(_("The case owner must be an internal user."))

    def _prepare_invoice_snapshot_commands(self):
        self.ensure_one()
        moves = self.subscription_id.invoice_ids.filtered(
            lambda move: move.state == "posted"
            and move.move_type in ("out_invoice", "out_refund", "out_receipt")
            and move.amount_residual
        ).sorted(key=lambda move: (move.invoice_date or move.date, move.id))
        commands = [fields.Command.clear()]
        for move in moves:
            invoice_date = move.invoice_date or move.date
            sign = -1.0 if move.move_type == "out_refund" else 1.0
            post_suspension = bool(invoice_date and self.suspended_on and invoice_date > self.suspended_on)
            commands.append(fields.Command.create({
                "move_id": move.id,
                "invoice_date": invoice_date,
                "due_date": move.invoice_date_due,
                "total_amount": sign * move.amount_total,
                "residual_amount": sign * move.amount_residual,
                "post_suspension": post_suspension,
                # Post-suspension charges require an explicit legal/accounting review.
                "include_in_claim": not post_suspension,
            }))
        return commands

    def action_refresh_snapshot(self):
        for case in self:
            if case.state not in ("review", "remediation"):
                raise UserError(_("The invoice snapshot is locked after a case is marked ready."))
            case.write({
                "invoice_line_ids": case._prepare_invoice_snapshot_commands(),
                "snapshot_date": fields.Date.context_today(case),
                "balance_reviewed": False,
                "post_suspension_reviewed": False,
                "manager_approved": False,
            })
        return True

    def action_mark_remediation(self):
        if any(case.state in ("final_demand", "counsel", "filed") for case in self) and not self.env.user.has_group(
            "contract_management.group_contract_litigation_manager"
        ):
            raise AccessError(_("Only a Litigation Manager may reopen a legally escalated case."))
        self.with_context(litigation_workflow_action=True).write({
            "state": "remediation",
            "manager_approved": False,
        })

    def action_mark_ready(self):
        for case in self:
            case._ensure_snapshot_current()
            if case.readiness_status != "ready":
                raise UserError(_("This case is not ready: %s") % (case.readiness_notes or _("unknown reason")))
            case.with_context(litigation_workflow_action=True).write({
                "state": "ready",
                "manager_approved": False,
            })
        return True

    def _check_collection_contact_window(self):
        timezone_name = self.env["ir.config_parameter"].sudo().get_param(
            "contract_management.litigation_timezone", "America/El_Salvador"
        )
        local_now = fields.Datetime.context_timestamp(
            self.with_context(tz=timezone_name), fields.Datetime.now()
        )
        if local_now.weekday() >= 5 or not (time(8, 0) <= local_now.time() < time(18, 0)):
            raise UserError(_("Collection communications may only be sent Monday-Friday, 8:00 AM-6:00 PM local time."))

    def _litigation_response_deadline(self):
        self.ensure_one()
        response_days = max(int(
            self.env["ir.config_parameter"].sudo().get_param(
                "contract_management.litigation_response_days", "10"
            )
        ), 1)
        return fields.Date.context_today(self) + timedelta(days=response_days)

    def _send_notice(self, template_xmlid, communication_type, next_state):
        template = self.env.ref(template_xmlid)
        for case in self:
            case._check_collection_contact_window()
            if not case.contact_email:
                raise UserError(_("A verified customer email is required before sending a notice."))
            deadline = case._litigation_response_deadline()
            mail_id, ticket, mail_message = case._queue_litigation_email_on_ticket(
                template,
                case.contact_email,
                deadline,
            )
            updates = {
                "state": next_state,
                "notice_deadline": deadline,
            }
            if communication_type == "initial_notice":
                updates["manager_approved"] = False
            case.with_context(litigation_workflow_action=True).write(updates)
            self.env["contract.litigation.communication"].create({
                "case_id": case.id,
                "communication_type": communication_type,
                "channel": "email",
                "direction": "outbound",
                "sent_on": fields.Datetime.now(),
                "recipient": case.contact_email,
                "status": "queued",
                "user_id": self.env.user.id,
                "helpdesk_ticket_id": ticket.id,
                "mail_message_id": mail_message.id,
                "source_key": "mail.message:%s" % mail_message.id if mail_message else False,
                "notes": _("Queued in Odoo outgoing email as message #%s.") % mail_id,
            })
        return True

    def action_send_initial_notice(self):
        self._ensure_initial_notice_ready()
        sms_failures = []
        missing_dte_invoices = []
        for case in self:
            case._check_collection_contact_window()
            deadline = case._litigation_response_deadline()
            customer_case = case.with_context(
                lang=case._litigation_customer_language()
            )
            if not case.contact_email:
                raise UserError(_("A verified customer email is required before sending the notice."))
            if "whatsapp.comm" not in self.env:
                raise UserError(_("Install WhatsApp Communication before sending the initial notice."))
            if "sms.comm" not in self.env:
                raise UserError(_("Install SMS Communications before sending the initial notice."))

            whatsapp_number = case._litigation_whatsapp_number()
            if not whatsapp_number:
                raise UserError(_(
                    "Add a WhatsApp or mobile number to the customer or the litigation case before sending the notice."
                ))
            whatsapp_helper = self.env["whatsapp.comm"].sudo()
            normalized_whatsapp = whatsapp_helper.normalize_phone(whatsapp_number)
            if not normalized_whatsapp:
                raise UserError(_("The customer's WhatsApp number is invalid."))
            language = LITIGATION_LANGUAGE
            template_name = case._litigation_whatsapp_template_name()
            case._ensure_litigation_whatsapp_template(template_name, language)

            sms_helper = self.env["sms.comm"].sudo()
            sms_number = sms_helper._recipient_number(case.partner_id, "billing")
            if not sms_number:
                raise UserError(_("Add a billing SMS or mobile number to the customer before sending the initial notice."))

            attachment, media_url, case_missing_dtes = (
                customer_case._litigation_notice_delivery_document()
            )
            missing_dte_invoices.extend(case_missing_dtes)

            # Queue the email with the same consolidated PDF used by WhatsApp.
            # It remains uncommitted, and therefore cannot be processed, until
            # the WhatsApp and SMS attempts are recorded.
            mail_template = self.env.ref(
                "contract_management.mail_template_litigation_initial_notice"
            )
            mail_id, ticket, mail_message = case._queue_litigation_email_on_ticket(
                mail_template,
                case.contact_email,
                deadline,
                attachment_ids=[attachment.id],
            )

            whatsapp_components = customer_case._litigation_whatsapp_components(
                media_url, attachment.name, deadline=deadline
            )
            whatsapp_helper._send_cloud_template(
                to_phone="+%s" % normalized_whatsapp,
                template_name=template_name,
                language_code=language,
                components=whatsapp_components,
                log_vals={
                    "partner_id": case.partner_id.id,
                    "sale_order": case.subscription_id.id,
                    "template_name": template_name,
                },
            )

            sms = sms_helper.send_mandatory_event(
                case.partner_id,
                customer_case._litigation_initial_sms_text(deadline),
                "litigation_initial_notice",
                related={"sale_order": case.subscription_id.id},
                category="billing",
            )
            sms_failed = sms.status == "ERR"
            if sms_failed:
                sms_failures.append(case.name)

            if not case.contact_whatsapp:
                case.contact_whatsapp = whatsapp_number
            case.with_context(litigation_workflow_action=True).write({
                "state": "initial_notice",
                "notice_deadline": deadline,
                "manager_approved": False,
            })
            Communication = self.env["contract.litigation.communication"]
            Communication.create({
                "case_id": case.id,
                "communication_type": "initial_notice",
                "channel": "email",
                "direction": "outbound",
                "sent_on": fields.Datetime.now(),
                "recipient": case.contact_email,
                "status": "queued",
                "user_id": self.env.user.id,
                "helpdesk_ticket_id": ticket.id,
                "mail_message_id": mail_message.id,
                "source_key": "mail.message:%s" % mail_message.id if mail_message else False,
                "notes": _("Queued in Odoo outgoing email as message #%s.") % mail_id,
            })
            Communication.create({
                "case_id": case.id,
                "communication_type": "initial_notice",
                "channel": "whatsapp",
                "direction": "outbound",
                "sent_on": fields.Datetime.now(),
                "recipient": whatsapp_number,
                "status": "sent",
                "user_id": self.env.user.id,
                "notes": _("Formal notice attached: %s") % attachment.name,
            })
            Communication.create({
                "case_id": case.id,
                "communication_type": "initial_notice",
                "channel": "sms",
                "direction": "outbound",
                "sent_on": fields.Datetime.now(),
                "recipient": sms_number,
                "status": "failed" if sms_failed else "sent",
                "user_id": self.env.user.id,
                "notes": _("SMS provider reference: %s. %s") % (
                    sms.referencia or _("not assigned"),
                    sms.status_text or "",
                ),
            })

        message = _("Initial notice queued by email and sent by WhatsApp and SMS.")
        if sms_failures:
            message += " " + _("SMS failed for: %s.") % ", ".join(sms_failures)
        if missing_dte_invoices:
            message += " " + _(
                "Warning: package produced without Hacienda DTE copies for: %s."
            ) % ", ".join(missing_dte_invoices)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Initial Notice"),
                "message": message,
                "type": "warning" if sms_failures or missing_dte_invoices else "success",
                "sticky": bool(sms_failures or missing_dte_invoices),
            },
        }

    def _ensure_initial_notice_ready(self):
        for case in self:
            case._ensure_snapshot_current()
            if case.state != "ready" or case.readiness_status != "ready":
                raise UserError(_("Only a fully reviewed, ready case can receive an initial notice."))
        return True

    @api.model
    def _litigation_azure_mail_server(self, raise_if_missing=True):
        server = self.env["ir.mail_server"].sudo().search([
            ("active", "=", True),
            ("smtp_host", "=", LITIGATION_AZURE_SMTP_HOST),
            ("smtp_user", "=", LITIGATION_AZURE_SMTP_USER),
        ], order="sequence, id", limit=1)
        if not server and raise_if_missing:
            raise UserError(_(
                "The Azure litigation email service is not configured. Expected an active SMTP service "
                "authenticated as notifications@cabal.sv on smtp.azurecomm.net."
            ))
        return server

    @api.model
    def _configure_litigation_email_templates(self):
        server = self._litigation_azure_mail_server(raise_if_missing=False)
        if not server:
            _logger.warning(
                "Litigation templates could not be bound to Azure SMTP because %s / %s was not found.",
                LITIGATION_AZURE_SMTP_HOST,
                LITIGATION_AZURE_SMTP_USER,
            )
            return False
        templates = self.env["mail.template"].sudo().browse([
            self.env.ref("contract_management.mail_template_litigation_initial_notice").id,
            self.env.ref("contract_management.mail_template_litigation_final_demand").id,
        ])
        templates.write({
            "email_from": LITIGATION_EMAIL_FROM,
            "reply_to": LITIGATION_EMAIL_REPLY_TO,
            "mail_server_id": server.id,
        })
        return True

    def _litigation_email_values(self, recipient, attachment_ids=None):
        self.ensure_one()
        server = self._litigation_azure_mail_server()
        values = {
            "email_to": recipient,
            "email_from": LITIGATION_EMAIL_FROM,
            "reply_to": LITIGATION_EMAIL_REPLY_TO,
            "mail_server_id": server.id,
        }
        if attachment_ids:
            values["attachment_ids"] = [(4, attachment_id) for attachment_id in attachment_ids]
        return values

    def _litigation_whatsapp_template_name(self):
        self.ensure_one()
        return (
            LITIGATION_WHATSAPP_CONTRACT_TEMPLATE
            if self.has_contract_evidence and self.pagare_verified
            else LITIGATION_WHATSAPP_BALANCE_TEMPLATE
        )

    def _litigation_whatsapp_number(self):
        self.ensure_one()
        partner_whatsapp = (
            self.partner_id.whatsapp if "whatsapp" in self.partner_id._fields else False
        )
        return self.contact_whatsapp or partner_whatsapp or self.partner_id.mobile or self.partner_id.phone

    def _litigation_customer_language(self):
        """Return the customer's language for externally visible communications."""
        self.ensure_one()
        return self.partner_id.lang or self.env.lang or LITIGATION_LANGUAGE

    def _litigation_customer_name(self):
        self.ensure_one()
        return self.partner_id.name or _("customer")

    def _litigation_selection_label(self, record, field_name):
        """Return a translated selection label for QWeb and generated documents."""
        self.ensure_one()
        field = record._fields[field_name]
        selection = dict(field._description_selection(record.env))
        return selection.get(record[field_name], record[field_name] or "")

    def _litigation_yes_no(self, value):
        self.ensure_one()
        return _("Yes") if value else _("No")

    def _litigation_enforcement_basis(self):
        self.ensure_one()
        if self.has_contract_evidence:
            return _("Pagaré sin protesto contained in the signed contract")
        if self.has_contract:
            return _("Account evidence; contract record exists without available contract evidence")
        return _("Account evidence (no contract record)")

    def _litigation_initial_notice_title(self):
        self.ensure_one()
        if self.has_contract_evidence and self.pagare_verified:
            return _("Formal demand: account remediation or termination")
        return _("Formal demand for payment")

    def _litigation_initial_notice_subject(self):
        self.ensure_one()
        account = self.subscription_id.cabal_sequence or self.subscription_id.name
        if self.has_contract_evidence and self.pagare_verified:
            return _("Formal demand: account remediation or termination — Contract %(account)s") % {
                "account": account,
            }
        return _("Formal demand for payment — Account %(account)s") % {
            "account": account,
        }

    def _litigation_final_demand_subject(self):
        self.ensure_one()
        account = self.subscription_id.cabal_sequence or self.subscription_id.name
        return _("Final demand for payment — Contract %(account)s") % {
            "account": account,
        }

    def _litigation_package_report_filename(self):
        self.ensure_one()
        return _("Litigation Package - %s") % self.name

    def _litigation_initial_notice_report_filename(self):
        self.ensure_one()
        return _("Formal demand - %s") % self.name

    def _format_litigation_amount(self, amount):
        self.ensure_one()
        return (
            format_amount(self.env, amount, self.currency_id)
            .replace("\u00a0", " ")
            .replace("\u202f", " ")
        )

    def _format_litigation_date(self, value):
        self.ensure_one()
        return format_date(self.env, fields.Date.to_date(value)) if value else ""

    def _format_litigation_long_date(self, value):
        self.ensure_one()
        if not value:
            return ""
        return format_date(
            self.env,
            fields.Date.to_date(value),
            date_format="long",
        )

    def _litigation_initial_sms_text(self, deadline=None):
        self.ensure_one()
        deadline = deadline or self._litigation_response_deadline()
        account = (self.subscription_id.cabal_sequence or self.subscription_id.name or self.name)[:17]
        return _(
            "CABAL: Requerimiento formal %(account)s. Saldo %(balance)s. "
            "Pague o responda antes del %(deadline)s. Sin respuesta: cobros y/o "
            "tribunales. Revise email/WhatsApp."
        ) % {
            "account": account,
            "balance": self._format_litigation_amount(self.claim_balance),
            "deadline": self._format_litigation_date(deadline),
        }

    def _litigation_whatsapp_components(self, media_url, filename, deadline=None):
        self.ensure_one()
        deadline = deadline or self._litigation_response_deadline()
        account = self.subscription_id.cabal_sequence or self.subscription_id.name or self.name
        values = [
            self.partner_id.name or _("Customer"),
            account,
            self._format_litigation_amount(self.claim_balance),
            self._format_litigation_date(deadline),
        ]
        return [
            {
                "type": "header",
                "parameters": [{
                    "type": "document",
                    "document": {"link": media_url, "filename": filename},
                }],
            },
            {
                "type": "body",
                "parameters": [{"type": "text", "text": value} for value in values],
            },
        ]

    def _litigation_notice_delivery_document(self):
        self.ensure_one()
        report = self.env.ref("contract_management.action_report_litigation_initial_notice")
        notice_pdf, _report_type = report.with_context(
            lang=self._litigation_customer_language()
        )._render_qweb_pdf(report.report_name, res_ids=self.ids)
        pdf_parts = [(_("Formal initial notice"), notice_pdf)]
        dte_parts, missing_dte_invoices = self._litigation_overdue_dte_pdf_parts(
            return_missing=True
        )
        pdf_parts.extend(dte_parts)
        pdf_parts.extend(self._litigation_contract_pdf_parts())
        pdf_content = self._merge_litigation_pdf_parts(pdf_parts)
        filename = _("Formal demand - %s.pdf") % self._package_filename(self.name)
        attachment = self.env["ir.attachment"].sudo().create({
            "name": filename,
            "type": "binary",
            "datas": base64.b64encode(pdf_content),
            "mimetype": "application/pdf",
            "res_model": self._name,
            "res_id": self.id,
            "description": _("Formal initial litigation notice prepared for WhatsApp delivery."),
        })
        token = attachment.access_token or attachment.generate_access_token()
        if isinstance(token, (list, tuple, set)):
            token = next(iter(token), "")
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        if not token or not base_url:
            raise UserError(_("Could not create a secure link for the notice document."))
        media_url = "%s/web/content/%s/%s?download=1&access_token=%s" % (
            base_url.rstrip("/"), attachment.id, quote(attachment.name), quote(str(token))
        )
        return attachment, media_url, missing_dte_invoices

    @staticmethod
    def _pdf_attachment_content(attachment):
        if not attachment or not attachment.datas:
            return False
        name = (attachment.name or "").lower()
        if attachment.mimetype != "application/pdf" and not name.endswith(".pdf"):
            return False
        return base64.b64decode(attachment.datas)

    def _litigation_overdue_dte_pdf_parts(self, return_missing=False):
        self.ensure_one()
        parts = []
        missing_invoices = []
        dte_model_available = "rodoo.dte" in self.env
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/")
        claim_lines = self.invoice_line_ids.filtered("include_in_claim").sorted(
            key=lambda line: (line.invoice_date or fields.Date.from_string("1900-01-01"), line.id)
        )
        for line in claim_lines:
            move = line.move_id
            label = _("Overdue DTE %s") % (move.name or move.id)
            dte_record = (
                self.env["rodoo.dte"].sudo().search([("move_id", "=", move.id)], limit=1)
                if dte_model_available
                else False
            )
            generation_code = getattr(move, "sv_numdoc", False)
            if not dte_record or not generation_code or not base_url:
                missing_invoices.append(move.display_name)
                continue

            dte_url = "%s/dte/pdf/%s/%s.pdf" % (
                base_url,
                move.id,
                quote(str(generation_code), safe=""),
            )
            try:
                # Fetch the exact public PDF path supplied to Meta by
                # cabal_send_dte_whatsapp.
                response = requests.get(dte_url, timeout=60)
                response.raise_for_status()
                pdf_content = response.content
            except Exception:
                _logger.exception(
                    "%s: failed to fetch the Hacienda DTE PDF for invoice %s.",
                    self.display_name,
                    move.display_name,
                )
                missing_invoices.append(move.display_name)
                continue

            if not pdf_content or not pdf_content.startswith(b"%PDF"):
                missing_invoices.append(move.display_name)
                continue
            filename = "%s.pdf" % generation_code
            parts.append(("%s - %s" % (label, filename), pdf_content))

        if missing_invoices:
            warning = _(
                "Warning: the official Hacienda DTE PDF was unavailable for the following overdue "
                "invoice(s): %(invoices)s. The package was produced without those invoice copies."
            ) % {"invoices": ", ".join(missing_invoices)}
            self.message_post(body=warning, subtype_xmlid="mail.mt_note")
            _logger.warning("%s: %s", self.display_name, warning)

        if return_missing:
            return parts, missing_invoices
        return parts

    def _litigation_contract_pdf_parts(self):
        self.ensure_one()
        if not self.has_contract_evidence:
            return []
        contract = self.applicable_contract_id
        if not contract:
            return []

        candidates = contract.signed_document_ids.filtered(
            lambda attachment: bool(self._pdf_attachment_content(attachment))
        )
        if candidates:
            return [
                (_("Signed contract %s") % attachment.name, self._pdf_attachment_content(attachment))
                for attachment in candidates
            ]
        if contract.contract_file:
            return [(
                _("Contract %s") % (contract.contract_filename or contract.display_name),
                base64.b64decode(contract.contract_file),
            )]
        if contract.docusign_id:
            candidates = contract.docusign_id.attachment_ids.filtered(
                lambda attachment: bool(self._pdf_attachment_content(attachment))
            )
        if candidates:
            return [
                (_("Contract %s") % attachment.name, self._pdf_attachment_content(attachment))
                for attachment in candidates
            ]
        raise UserError(_(
            "The applicable contract does not have a PDF available to attach to the initial notice."
        ))

    @staticmethod
    def _merge_litigation_pdf_parts(pdf_parts):
        writer = PdfFileWriter()
        for label, pdf_content in pdf_parts:
            try:
                reader = PdfFileReader(io.BytesIO(pdf_content), strict=False)
                if reader.isEncrypted and reader.decrypt("") == 0:
                    raise ValueError(_("encrypted PDF"))
                for page_number in range(reader.getNumPages()):
                    writer.addPage(reader.getPage(page_number))
            except Exception as exc:
                raise UserError(_("The PDF '%(label)s' could not be added to the notice package: %(error)s") % {
                    "label": label,
                    "error": str(exc),
                }) from exc
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()

    def _ensure_litigation_whatsapp_template(self, template_name, language):
        self.ensure_one()
        if "whatsapp.comm" not in self.env or "whatsapp.comm.config.templates" not in self.env:
            raise UserError(_("Install WhatsApp Communication before sending litigation notices by WhatsApp."))
        template = self.env["whatsapp.comm.config.templates"].sudo().search([
            ("template", "=", template_name),
        ], limit=1)
        if not template:
            raise UserError(_("Configure and submit the litigation WhatsApp templates from Contract Management settings first."))
        language_code = self.env["whatsapp.comm"]._normalize_cloud_template_language(language)

        def status_for_language():
            try:
                metadata = json.loads(template.meta_template_ids_json or "{}")
            except (TypeError, ValueError):
                metadata = {}
            return (metadata.get(language_code) or {}).get("status")

        status = status_for_language()
        if status != "APPROVED":
            template.action_sync_templates_from_meta()
            template.invalidate_recordset(["meta_template_ids_json"])
            status = status_for_language()
        if status != "APPROVED":
            raise UserError(_(
                "The litigation WhatsApp template '%s' is not approved by Meta yet (status: %s)."
            ) % (template_name, status or _("not available")))

    def action_send_initial_notice_whatsapp(self):
        self._ensure_initial_notice_ready()
        missing_dte_invoices = []
        for case in self:
            case._check_collection_contact_window()
            whatsapp_number = case._litigation_whatsapp_number()
            if not whatsapp_number:
                raise UserError(_(
                    "Add a WhatsApp or mobile number to the customer or the litigation case before sending the notice."
                ))
            if not case.contact_whatsapp:
                case.contact_whatsapp = whatsapp_number
            if "whatsapp.comm" not in self.env:
                raise UserError(_("Install WhatsApp Communication before sending litigation notices by WhatsApp."))
            helper = self.env["whatsapp.comm"].sudo()
            normalized_phone = helper.normalize_phone(whatsapp_number)
            if not normalized_phone:
                raise UserError(_("The customer's WhatsApp number is invalid."))
            language = LITIGATION_LANGUAGE
            template_name = case._litigation_whatsapp_template_name()
            case._ensure_litigation_whatsapp_template(template_name, language)
            deadline = case._litigation_response_deadline()
            attachment, media_url, case_missing_dtes = case._litigation_notice_delivery_document()
            missing_dte_invoices.extend(case_missing_dtes)
            components = case._litigation_whatsapp_components(
                media_url, attachment.name, deadline=deadline
            )
            helper._send_cloud_template(
                to_phone="+%s" % normalized_phone,
                template_name=template_name,
                language_code=language,
                components=components,
                log_vals={
                    "partner_id": case.partner_id.id,
                    "sale_order": case.subscription_id.id,
                    "template_name": template_name,
                },
            )
            case.with_context(litigation_workflow_action=True).write({
                "state": "initial_notice",
                "notice_deadline": deadline,
                "manager_approved": False,
            })
            self.env["contract.litigation.communication"].create({
                "case_id": case.id,
                "communication_type": "initial_notice",
                "channel": "whatsapp",
                "sent_on": fields.Datetime.now(),
                "recipient": whatsapp_number,
                "status": "sent",
                "user_id": self.env.user.id,
                "notes": _("Formal notice attached: %s") % attachment.name,
            })
        if missing_dte_invoices:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Initial Notice Sent with Warning"),
                    "message": _(
                        "The notice was sent, but the package omitted unavailable Hacienda DTE copies for: %s."
                    ) % ", ".join(missing_dte_invoices),
                    "type": "warning",
                    "sticky": True,
                },
            }
        return True

    def action_send_final_demand(self):
        if not self.env.user.has_group("contract_management.group_contract_litigation_manager"):
            raise AccessError(_("Only a Litigation Manager may send a final demand."))
        today = fields.Date.context_today(self)
        for case in self:
            case._ensure_snapshot_current()
            if case.readiness_status != "ready":
                raise UserError(_("This case is no longer eligible: %s") % case.readiness_notes)
            if case.state != "initial_notice":
                raise UserError(_("The initial notice must be sent before the final demand."))
            if case.notice_deadline and case.notice_deadline > today:
                raise UserError(_("The initial notice response deadline has not passed."))
            if not case.manager_approved:
                raise UserError(_("Manager approval is required before the final demand."))
        return self._send_notice(
            "contract_management.mail_template_litigation_final_demand",
            "final_demand",
            "final_demand",
        )

    def action_refer_to_counsel(self):
        if not self.env.user.has_group("contract_management.group_contract_litigation_manager"):
            raise AccessError(_("Only a Litigation Manager may refer a case to counsel."))
        for case in self:
            case._ensure_snapshot_current()
            case._ensure_pagare_enforcement_ready()
            if case.legal_hold or case.subscription_id.subscription_state != "8_suspend":
                raise UserError(_("The subscription is no longer eligible for legal referral."))
            if case.state != "final_demand" or not case.manager_approved:
                raise UserError(_("A manager-approved final demand is required before referral."))
            case.with_context(litigation_workflow_action=True).write({
                "state": "counsel",
                "referred_on": fields.Date.context_today(case),
            })
        return True

    def action_mark_filed(self):
        if not self.env.user.has_group("contract_management.group_contract_litigation_manager"):
            raise AccessError(_("Only a Litigation Manager may mark a case filed."))
        for case in self:
            case._ensure_snapshot_current()
            case._ensure_pagare_enforcement_ready()
            if case.legal_hold or case.subscription_id.subscription_state != "8_suspend":
                raise UserError(_("The subscription is no longer eligible for filing."))
            if case.state != "counsel":
                raise UserError(_("Only a case referred to counsel may be marked filed."))
            if not case.court_reference:
                raise UserError(_("Enter the court or counsel reference before marking the case filed."))
            case.with_context(litigation_workflow_action=True).write({
                "state": "filed",
                "filed_on": fields.Date.context_today(case),
            })
        return True

    def action_mark_settled(self):
        if not self.env.user.has_group("contract_management.group_contract_litigation_manager"):
            raise AccessError(_("Only a Litigation Manager may record a settlement."))
        for case in self:
            if case.state not in ("initial_notice", "final_demand", "counsel", "filed"):
                raise UserError(_("Only a communicated or legally escalated case may be settled."))
            if float_compare(
                case.settlement_amount,
                0.0,
                precision_rounding=case.currency_id.rounding,
            ) <= 0:
                raise UserError(_("Enter the agreed settlement amount before marking the case settled."))
            case.with_context(litigation_workflow_action=True).write({
                "state": "settled",
                "settled_on": fields.Date.context_today(case),
                "closure_reason": "settled",
            })
        return True

    def action_close_case(self):
        if not self.env.user.has_group("contract_management.group_contract_litigation_manager"):
            raise AccessError(_("Only a Litigation Manager may close a case."))
        for case in self:
            if not case.closure_reason:
                raise UserError(_("Select a closure reason before closing the case."))
            case.with_context(litigation_workflow_action=True).write({"state": "closed"})
        return True

    def action_print_package(self):
        self.ensure_one()
        self._ensure_pagare_enforcement_ready()
        return self.env.ref("contract_management.action_report_litigation_package").report_action(self)

    def _ensure_pagare_enforcement_ready(self):
        """Require pagaré verification only when contract evidence will be enforced."""
        self.ensure_one()
        if self.has_contract_evidence and not self.pagare_verified:
            raise UserError(
                _("Verify the customer-signed pagaré in the contract before generating the litigation package.")
            )

    def _package_filename(self, value, fallback=None):
        self.ensure_one()
        fallback = fallback or _("document")
        filename = re.sub(r"[^A-Za-z0-9._-]+", "_", value or fallback).strip("._")
        return filename or fallback

    def _package_add_attachment(self, archive, attachment, folder=None):
        self.ensure_one()
        folder = folder or _("Supporting")
        if not attachment.datas:
            return
        filename = self._package_filename(attachment.name, "attachment_%s" % attachment.id)
        archive.writestr("%s/%s_%s" % (folder, attachment.id, filename), base64.b64decode(attachment.datas))

    def action_generate_package_archive(self):
        self.ensure_one()
        if self.state in ("review", "remediation", "cancelled"):
            raise UserError(_("Complete the evidence review before generating the litigation archive."))
        self._ensure_snapshot_current()
        self._ensure_pagare_enforcement_ready()

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            cover_report = self.env.ref("contract_management.action_report_litigation_package")
            cover_pdf = cover_report._render_qweb_pdf(
                report_ref="contract_management.action_report_litigation_package",
                res_ids=self.ids,
            )[0]
            archive.writestr(
                _("00_Case_Summary_%s.pdf") % self._package_filename(self.name),
                cover_pdf,
            )

            dte_parts, _missing_dte_invoices = self._litigation_overdue_dte_pdf_parts(
                return_missing=True
            )
            for index, (label, invoice_pdf) in enumerate(dte_parts, start=1):
                invoice_name = self._package_filename(label, "official_dte_%s" % index)
                archive.writestr(
                    _("Invoices/%(index)02d_%(invoice)s.pdf") % {
                        "index": index,
                        "invoice": invoice_name,
                    },
                    invoice_pdf,
                )

            seen_attachment_ids = set()
            for contract in self.subscription_id.contract_ids:
                for attachment in contract.signed_document_ids:
                    if attachment.id not in seen_attachment_ids:
                        self._package_add_attachment(
                            archive,
                            attachment,
                            folder=_("Contracts_and_Pagare"),
                        )
                        seen_attachment_ids.add(attachment.id)
                if contract.contract_file:
                    filename = self._package_filename(
                        contract.contract_filename,
                        "contract_%s.pdf" % contract.id,
                    )
                    archive.writestr(
                        _("Contracts_and_Pagare/%(contract)s_%(filename)s") % {
                            "contract": contract.id,
                            "filename": filename,
                        },
                        base64.b64decode(contract.contract_file),
                    )
                for addendum in contract.addendum_ids.filtered(
                    lambda item: item.state in ("signed", "active")
                ):
                    for attachment in addendum.signed_document_ids:
                        if attachment.id not in seen_attachment_ids:
                            self._package_add_attachment(
                                archive,
                                attachment,
                                folder=_("Contracts_and_Pagare/Signed_Addenda"),
                            )
                            seen_attachment_ids.add(attachment.id)

            supporting = self.env["ir.attachment"].search([
                ("res_model", "=", self._name),
                ("res_id", "=", self.id),
                ("id", "!=", self.package_attachment_id.id),
            ])
            for attachment in supporting:
                self._package_add_attachment(archive, attachment)

        package_name = _("Litigation_Package_%s.zip") % self._package_filename(self.name)
        attachment_values = {
            "name": package_name,
            "type": "binary",
            "datas": base64.b64encode(buffer.getvalue()),
            "mimetype": "application/zip",
            "res_model": self._name,
            "res_id": self.id,
            "description": _("Generated litigation evidence package."),
        }
        if self.package_attachment_id:
            self.package_attachment_id.sudo().write(attachment_values)
            attachment = self.package_attachment_id
        else:
            attachment = self.env["ir.attachment"].sudo().create(attachment_values)
        self.write({
            "package_attachment_id": attachment.id,
            "package_generated_on": fields.Datetime.now(),
        })
        self.message_post(body=_("Litigation package generated: %s") % package_name)
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s?download=true" % attachment.id,
            "target": "self",
        }

    @api.model
    def _configure_litigation_whatsapp_templates(self):
        if "whatsapp.comm.config.templates" not in self.env:
            raise UserError(_("Install WhatsApp Communication before configuring litigation templates."))
        languages = self.env["res.lang"].sudo().with_context(active_test=False).search([
            ("code", "in", ["es_419", "es_ES"]),
        ])
        model_id = self.env["ir.model"]._get_id(self._name)
        configured = self.env["whatsapp.comm.config.templates"]
        definitions_by_name = {
            LITIGATION_WHATSAPP_CONTRACT_TEMPLATE: 4,
            LITIGATION_WHATSAPP_BALANCE_TEMPLATE: 4,
        }
        for template_name, body_count in definitions_by_name.items():
            template = self.env["whatsapp.comm.config.templates"].sudo().search([
                ("template", "=", template_name),
            ], limit=1)
            values = {
                "template": template_name,
                "label": _("Litigation Initial Notice - Contract")
                if template_name == LITIGATION_WHATSAPP_CONTRACT_TEMPLATE
                else _("Litigation Initial Notice - Balance Only"),
                "category": "utility",
                "model": model_id,
                "notification_type": "litigation_initial_notice",
                "language_ids": [(6, 0, languages.ids)],
            }
            if template:
                template.sudo().write(values)
            else:
                template = self.env["whatsapp.comm.config.templates"].sudo().create(values)
            definitions = [{
                "component_type": "header",
                "button_index": 0,
                "position": 1,
                "parameter_name": False,
                "parameter_type": "document",
            }]
            definitions.extend({
                "component_type": "body",
                "button_index": 0,
                "position": position,
                "parameter_name": False,
                "parameter_type": "text",
            } for position in range(1, body_count + 1))
            template._sync_meta_parameter_definitions(definitions)
            configured |= template
        return configured

    @api.model
    def _litigation_whatsapp_template_bodies(self):
        contract_body = _(
            "*REQUERIMIENTO FORMAL*\n\n"
            "{{1}}, la cuenta {{2}} mantiene un saldo vencido de {{3}}. "
            "A más tardar el {{4}} debe elegir y formalizar una de estas opciones:\n\n"
            "1. Pagar el saldo o acordar un plan de pagos para restablecer el servicio "
            "por el plazo restante del contrato.\n"
            "2. Terminar el contrato y pagar el monto que resulte exigible conforme al pagaré firmado, "
            "después de aplicar los pagos, créditos y ajustes que correspondan.\n\n"
            "Si no recibimos pago, convenio, elección escrita u objeción documentada dentro del plazo, "
            "el expediente será remitido, sin nuevo aviso, a una agencia de cobros y/o a los tribunales "
            "competentes para el cobro y la ejecución que corresponda. Consulte el requerimiento adjunto."
        )
        balance_body = _(
            "*REQUERIMIENTO FORMAL DE PAGO*\n\n"
            "{{1}}, la cuenta {{2}} mantiene un saldo vencido de {{3}}. "
            "Le requerimos pagarlo íntegramente a más tardar el {{4}} o presentar una objeción documentada.\n\n"
            "Si no recibimos el pago o una objeción documentada dentro del plazo, el expediente será remitido, "
            "sin nuevo aviso, a una agencia de cobros y/o a los tribunales competentes para el cobro del saldo. "
            "Consulte el requerimiento adjunto."
        )
        return {
            LITIGATION_WHATSAPP_CONTRACT_TEMPLATE: (
                contract_body,
                [_('Sample Customer'), "SUB-0001", "$125.00", "17/08/2026"],
            ),
            LITIGATION_WHATSAPP_BALANCE_TEMPLATE: (
                balance_body,
                [_('Sample Customer'), "SUB-0001", "$125.00", "17/08/2026"],
            ),
        }

    @api.model
    def _litigation_whatsapp_sample_pdf(self):
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas

        buffer = io.BytesIO()
        document = canvas.Canvas(buffer, pagesize=letter)
        document.setTitle(_("Formal demand - sample"))
        document.setFont("Helvetica-Bold", 15)
        document.drawString(72, 720, _("FORMAL DEMAND - SAMPLE DOCUMENT"))
        document.setFont("Helvetica", 10)
        document.drawString(
            72,
            690,
            _("This sample document is used only to register the template."),
        )
        document.drawString(
            72,
            672,
            _("The customer notice will include the applicable statement and demand."),
        )
        document.save()
        return buffer.getvalue()

    @api.model
    def _raise_litigation_meta_error(self, response, operation):
        if response.ok:
            return
        try:
            error = (response.json().get("error") or {}).get("message")
        except ValueError:
            error = response.text
        raise UserError(_("Meta rejected the request while %s (HTTP %s): %s") % (
            operation, response.status_code, error or response.reason
        ))

    @api.model
    def _upload_litigation_whatsapp_sample(self, api_version, app_id, token, pdf_content):
        session_response = requests.post(
            "https://graph.facebook.com/%s/%s/uploads" % (api_version, app_id),
            params={
                "file_name": "litigation-notice-sample.pdf",
                "file_length": len(pdf_content),
                "file_type": "application/pdf",
                "access_token": token,
            },
            timeout=30,
        )
        self._raise_litigation_meta_error(session_response, _("creating the sample upload"))
        upload_id = session_response.json().get("id")
        if not upload_id:
            raise UserError(_("Meta did not return a sample upload session."))
        upload_response = requests.post(
            "https://graph.facebook.com/%s/%s" % (api_version, upload_id),
            headers={
                "Authorization": "OAuth %s" % token,
                "file_offset": "0",
                "Content-Type": "application/octet-stream",
            },
            data=pdf_content,
            timeout=30,
        )
        self._raise_litigation_meta_error(upload_response, _("uploading the sample PDF"))
        handle = upload_response.json().get("h")
        if not handle:
            raise UserError(_("Meta did not return a document header handle."))
        return handle

    @api.model
    def action_submit_litigation_whatsapp_templates(self):
        if not self.env.user.has_group("contract_management.group_contract_litigation_manager"):
            raise AccessError(_("Only a Litigation Manager may submit litigation WhatsApp templates."))
        configured = self._configure_litigation_whatsapp_templates()
        params = self.env["ir.config_parameter"].sudo()
        api_version = (params.get_param("wa_cloud_api_version") or "v20.0").strip()
        app_id = (params.get_param("wa_cloud_app_id") or "").strip()
        business_account_id = (params.get_param("wa_cloud_business_account_id") or "").strip()
        if not app_id or not business_account_id:
            raise UserError(_("Configure the WhatsApp App ID and Business Account ID before submitting the litigation templates."))
        helper = self.env["whatsapp.comm"].sudo()
        token = helper.get_valid_cloud_token()
        header_handle = self._upload_litigation_whatsapp_sample(
            api_version, app_id, token, self._litigation_whatsapp_sample_pdf()
        )
        endpoint = "https://graph.facebook.com/%s/%s/message_templates" % (
            api_version, business_account_id
        )
        headers = {"Authorization": "Bearer %s" % token, "Content-Type": "application/json"}
        existing_response = requests.get(
            endpoint,
            headers=headers,
            params={"fields": "name,language,status", "limit": 100},
            timeout=30,
        )
        self._raise_litigation_meta_error(existing_response, _("checking existing templates"))
        existing = {
            (item.get("name"), item.get("language"))
            for item in (existing_response.json().get("data") or [])
        }
        submitted = []
        for template_name, (body, examples) in self._litigation_whatsapp_template_bodies().items():
            if (template_name, "es") in existing:
                continue
            payload = {
                "name": template_name,
                "language": "es",
                "category": "UTILITY",
                "allow_category_change": True,
                "components": [
                    {"type": "HEADER", "format": "DOCUMENT", "example": {"header_handle": [header_handle]}},
                    {"type": "BODY", "text": body, "example": {"body_text": [examples]}},
                    {"type": "FOOTER", "text": "Cabal Internet"},
                ],
            }
            response = requests.post(endpoint, headers=headers, json=payload, timeout=30)
            self._raise_litigation_meta_error(response, _("submitting template %s") % template_name)
            submitted.append(template_name)
        configured[:1].action_sync_templates_from_meta()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Litigation WhatsApp Templates"),
                "message": _("Submitted %s template(s). Existing templates were synchronized. Meta approval is required before sending.") % len(submitted),
                "type": "success",
                "sticky": True,
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    @api.model
    def cron_create_litigation_cases(self):
        """Create review cases only; this method never contacts customers."""
        params = self.env["ir.config_parameter"].sudo()
        threshold = max(int(params.get_param("contract_management.litigation_threshold_days", "90")), 1)
        minimum = max(
            float(params.get_param("contract_management.litigation_minimum_balance", "1.00")),
            0.0,
        )
        today = fields.Date.context_today(self)
        cutoff = today - timedelta(days=threshold)
        subscriptions = self.env["sale.order"].sudo().search([
            ("is_subscription", "=", True),
            ("subscription_state", "=", "8_suspend"),
            ("litigation_hold", "=", False),
        ])
        subscriptions._litigation_sync_suspension_date()
        subscriptions = subscriptions.filtered(
            lambda order: order.litigation_suspended_on and order.litigation_suspended_on <= cutoff
        )
        for subscription in subscriptions:
            if self.sudo().search_count([
                ("subscription_id", "=", subscription.id),
                ("suspended_on", "=", subscription.litigation_suspended_on),
            ]):
                continue
            case = self.sudo().create({
                "subscription_id": subscription.id,
                "suspended_on": subscription.litigation_suspended_on,
                "threshold_days": threshold,
            })
            if float_compare(case.gross_open_balance, minimum, precision_rounding=case.currency_id.rounding) < 0:
                case.unlink()
        return True


class ContractLitigationCaseInvoice(models.Model):
    _name = "contract.litigation.case.invoice"
    _description = "Litigation Case Invoice Snapshot"
    _order = "invoice_date, id"
    _check_company_auto = True

    case_id = fields.Many2one(
        "contract.litigation.case", required=True, ondelete="cascade", index=True
    )
    company_id = fields.Many2one(related="case_id.company_id", store=True)
    currency_id = fields.Many2one(related="case_id.currency_id", store=True)
    move_id = fields.Many2one("account.move", string="Invoice", required=True, ondelete="restrict")
    invoice_date = fields.Date(readonly=True)
    due_date = fields.Date(readonly=True)
    total_amount = fields.Monetary(currency_field="currency_id", readonly=True)
    residual_amount = fields.Monetary(currency_field="currency_id", readonly=True)
    post_suspension = fields.Boolean(readonly=True)
    include_in_claim = fields.Boolean(
        string="Include in Claim",
        help="Post-suspension items default to excluded and must be affirmatively reviewed.",
    )
    review_note = fields.Char()

    def write(self, vals):
        if any(line.case_id.state not in ("review", "remediation") for line in self):
            protected = {"include_in_claim", "review_note"}
            if protected.intersection(vals):
                raise UserError(_("Invoice claim decisions are locked after the case is marked ready."))
        return super().write(vals)


class ContractLitigationCommunication(models.Model):
    _name = "contract.litigation.communication"
    _description = "Litigation Case Communication"
    _order = "sent_on desc, id desc"

    case_id = fields.Many2one(
        "contract.litigation.case", required=True, ondelete="cascade", index=True
    )
    company_id = fields.Many2one(related="case_id.company_id", store=True)
    communication_type = fields.Selection(
        [
            ("initial_notice", "Initial Notice"),
            ("follow_up", "Follow-up"),
            ("final_demand", "Final Demand"),
            ("dispute", "Dispute"),
            ("settlement", "Settlement"),
            ("other", "Other"),
        ],
        required=True,
    )
    channel = fields.Selection(
        [
            ("email", "Email"),
            ("phone", "Phone"),
            ("whatsapp", "WhatsApp"),
            ("sms", "SMS"),
            ("physical", "Physical"),
        ],
        required=True,
    )
    sent_on = fields.Datetime(
        string="Queued / Sent On", required=True, default=fields.Datetime.now
    )
    recipient = fields.Char(required=True)
    status = fields.Selection(
        [
            ("draft", "Draft"),
            ("queued", "Queued"),
            ("sent", "Sent"),
            ("delivered", "Delivered"),
            ("failed", "Failed"),
        ],
        default="draft",
        required=True,
    )
    user_id = fields.Many2one("res.users", default=lambda self: self.env.user, required=True)
    notes = fields.Text()
