# -*- coding: utf-8 -*-

import logging

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html_escape

from .contract_litigation import (
    LITIGATION_EMAIL_FROM,
    LITIGATION_EMAIL_REPLY_TO,
)


_logger = logging.getLogger(__name__)

LITIGATION_ACTIVE_COMMUNICATION_STATES = (
    "initial_notice",
    "final_demand",
    "counsel",
    "filed",
    "settled",
    "closed",
)


class ContractLitigationCaseCommunications(models.Model):
    _inherit = "contract.litigation.case"

    helpdesk_ticket_ids = fields.One2many(
        "helpdesk.ticket",
        "litigation_case_id",
        string="Legal Collection Tickets",
        readonly=True,
    )
    helpdesk_ticket_id = fields.Many2one(
        "helpdesk.ticket",
        string="Primary Legal Collection Ticket",
        compute="_compute_legal_communication_summary",
    )
    legal_ticket_count = fields.Integer(
        string="Legal Tickets",
        compute="_compute_legal_communication_summary",
    )
    unread_communication_count = fields.Integer(
        string="Unread Customer Responses",
        compute="_compute_legal_communication_summary",
    )
    last_customer_response_on = fields.Datetime(
        string="Last Customer Response",
        compute="_compute_legal_communication_summary",
    )

    @api.depends(
        "helpdesk_ticket_ids",
        "communication_ids.direction",
        "communication_ids.is_unread",
        "communication_ids.sent_on",
    )
    def _compute_legal_communication_summary(self):
        for case in self:
            tickets = case.helpdesk_ticket_ids.sorted("id")
            case.helpdesk_ticket_id = tickets[:1]
            case.legal_ticket_count = len(tickets)
            inbound = case.communication_ids.filtered(
                lambda item: item.direction == "inbound"
            )
            case.unread_communication_count = len(inbound.filtered("is_unread"))
            case.last_customer_response_on = max(
                inbound.mapped("sent_on"), default=False
            )

    @api.model
    def _legal_helpdesk_team(self):
        return self.env.ref("contract_management.helpdesk_team_legal_collections")

    @api.model
    def _configure_legal_helpdesk_team(self):
        team = self._legal_helpdesk_team().sudo()
        domain = self.env["mail.alias.domain"].sudo().search([
            ("name", "=", "cabal.sv"),
        ], limit=1)
        reviewers = self.env.ref(
            "contract_management.group_contract_litigation_user"
        ).sudo().users.filtered(lambda user: user.active and not user.share)
        values = {
            "use_alias": True,
            "alias_name": "legal",
            "privacy_visibility": "invited_internal",
            "member_ids": [fields.Command.set(reviewers.ids)],
        }
        if domain:
            values["alias_domain_id"] = domain.id
        team.write(values)
        return True

    @api.model
    def _configure_legal_incoming_mailbox(self):
        """Use the existing Microsoft OAuth identity for the shared mailbox."""
        Server = self.env["fetchmail.server"].sudo()
        source = Server.search([
            ("server_type", "=", "outlook"),
            ("user", "=", "administracion@redeslitorales.com"),
            ("active", "=", True),
        ], limit=1)
        target = Server.search([
            ("server_type", "=", "outlook"),
            ("user", "=", "legal@cabal.sv"),
        ], limit=1)
        ticket_model = self.env["ir.model"].sudo()._get("helpdesk.ticket")
        values = {
            "name": _("Legal Collections"),
            "server_type": "outlook",
            "server": "imap.outlook.com",
            "port": 993,
            "is_ssl": True,
            "user": "legal@cabal.sv",
            "object_id": ticket_model.id,
            # Activate only after delegated IMAP access to the newly created
            # shared mailbox has been tested successfully.  A valid token for
            # the primary mailbox does not prove shared-mailbox permission.
            "active": target.active if target else False,
        }
        if source:
            for field_name in (
                "microsoft_outlook_refresh_token",
                "microsoft_outlook_access_token",
                "microsoft_outlook_access_token_expiration",
                "microsoft_outlook_uri",
            ):
                if field_name in Server._fields and source[field_name]:
                    values[field_name] = source[field_name]
            if "state" in Server._fields:
                values["state"] = "done"
        if target:
            # Preserve a token if this mailbox was authorized separately in Odoo.
            for field_name in (
                "microsoft_outlook_refresh_token",
                "microsoft_outlook_access_token",
                "microsoft_outlook_access_token_expiration",
                "microsoft_outlook_uri",
            ):
                if field_name in Server._fields and target[field_name]:
                    values.pop(field_name, None)
            target.write(values)
        else:
            target = Server.create(values)
        return target.id

    def _ensure_legal_helpdesk_ticket(self):
        self.ensure_one()
        ticket = self.helpdesk_ticket_ids.sorted("id")[:1]
        if ticket:
            values = {}
            if self.responsible_id and ticket.user_id != self.responsible_id:
                values["user_id"] = self.responsible_id.id
            if ticket.partner_id != self.partner_id:
                values["partner_id"] = self.partner_id.id
            if values:
                ticket.sudo().write(values)
            return ticket

        ticket = self.env["helpdesk.ticket"].sudo().create({
            "name": _("Legal Collections %(case)s - %(customer)s") % {
                "case": self.name,
                "customer": self.partner_id.display_name,
            },
            "team_id": self._legal_helpdesk_team().id,
            "user_id": self.responsible_id.id if self.responsible_id else False,
            "partner_id": self.partner_id.id,
            "partner_email": self.contact_email or self.partner_id.email,
            "partner_phone": (
                self.contact_phone
                or self.partner_id.phone
                or self.partner_id.mobile
            ),
            "litigation_case_id": self.id,
            "description": Markup("<p><strong>%s</strong></p><p>%s</p>") % (
                html_escape(_("Litigation case: %s") % self.name),
                html_escape(_(
                    "Use this ticket for all customer replies by email and Chatwoot. "
                    "The litigation case remains the legal system of record."
                )),
            ),
        })
        return ticket

    def _queue_litigation_email_on_ticket(
        self, template, recipient, deadline, attachment_ids=None
    ):
        self.ensure_one()
        ticket = self._ensure_legal_helpdesk_ticket()
        email_values = self._litigation_email_values(
            recipient, attachment_ids=attachment_ids
        )
        email_values.update({
            "model": "helpdesk.ticket",
            "res_id": ticket.id,
        })
        mail_id = template.with_context(
            response_deadline=deadline,
            skip_litigation_message_capture=True,
        ).send_mail(
            self.id,
            force_send=False,
            email_values=email_values,
        )
        mail = self.env["mail.mail"].sudo().browse(mail_id)
        return mail_id, ticket, mail.mail_message_id

    def action_open_legal_helpdesk_tickets(self):
        self.ensure_one()
        ticket = self._ensure_legal_helpdesk_ticket()
        action = {
            "type": "ir.actions.act_window",
            "name": _("Legal Collection Tickets"),
            "res_model": "helpdesk.ticket",
            "view_mode": "tree,form",
        }
        tickets = self.env["helpdesk.ticket"].search([
            ("litigation_case_id", "=", self.id),
        ], order="id")
        if len(tickets) == 1:
            action.update({
                "view_mode": "form",
                "views": [(False, "form")],
                "res_id": ticket.id,
            })
        else:
            action["domain"] = [("id", "in", tickets.ids)]
        return action

    def action_mark_communications_read(self):
        self.mapped("communication_ids").filtered(
            lambda item: item.direction == "inbound" and item.is_unread
        ).write({"is_unread": False})
        return True

    def write(self, vals):
        result = super().write(vals)
        if "responsible_id" in vals:
            for case in self:
                case.helpdesk_ticket_ids.sudo().write({
                    "user_id": (
                        case.responsible_id.id if case.responsible_id else False
                    ),
                })
        return result


class HelpdeskTicketLitigation(models.Model):
    _inherit = "helpdesk.ticket"

    partner_has_litigation_case = fields.Boolean(
        string="Customer Has Litigation Case",
        compute="_compute_partner_has_litigation_case",
        compute_sudo=True,
    )
    litigation_case_id = fields.Many2one(
        "contract.litigation.case",
        string="Litigation Case",
        index=True,
        ondelete="set null",
        tracking=True,
    )

    @api.depends("partner_id")
    def _compute_partner_has_litigation_case(self):
        commercial_partners = self.mapped(
            "partner_id.commercial_partner_id"
        )
        cases = self.env["contract.litigation.case"].sudo().search([
            (
                "partner_id.commercial_partner_id",
                "in",
                commercial_partners.ids,
            ),
        ]) if commercial_partners else self.env["contract.litigation.case"]
        partners_with_cases = cases.mapped(
            "partner_id.commercial_partner_id"
        )
        for ticket in self:
            ticket.partner_has_litigation_case = bool(
                ticket.partner_id
                and ticket.partner_id.commercial_partner_id
                in partners_with_cases
            )

    @api.model_create_multi
    def create(self, vals_list):
        tickets = super().create(vals_list)
        legal_team = self.env.ref(
            "contract_management.helpdesk_team_legal_collections",
            raise_if_not_found=False,
        )
        if not legal_team:
            return tickets
        for ticket in tickets.filtered(
            lambda item: item.team_id == legal_team
            and item.partner_id
            and not item.litigation_case_id
        ):
            ticket._auto_link_litigation_case()
        return tickets

    def _auto_link_litigation_case(self):
        self.ensure_one()
        if self.litigation_case_id or not self.partner_id:
            return self.litigation_case_id
        cases = self.env["contract.litigation.case"].sudo().search([
            ("partner_id", "child_of", self.partner_id.commercial_partner_id.id),
            ("state", "in", LITIGATION_ACTIVE_COMMUNICATION_STATES),
        ], limit=2)
        if len(cases) == 1:
            self.sudo().write({
                "litigation_case_id": cases.id,
                "user_id": self.user_id.id or cases.responsible_id.id,
            })
            return cases
        return self.env["contract.litigation.case"]

    def action_open_litigation_case(self):
        self.ensure_one()
        if not self.litigation_case_id:
            raise UserError(_("This ticket is not linked to a litigation case."))
        return {
            "type": "ir.actions.act_window",
            "name": self.litigation_case_id.display_name,
            "res_model": "contract.litigation.case",
            "view_mode": "form",
            "res_id": self.litigation_case_id.id,
        }


class ContractLitigationCommunicationDetails(models.Model):
    _inherit = "contract.litigation.communication"

    communication_type = fields.Selection(
        selection_add=[("customer_reply", "Customer Reply")],
        ondelete={
            "customer_reply": lambda records: records.write({
                "communication_type": "other"
            })
        },
    )
    direction = fields.Selection(
        [
            ("inbound", "Inbound"),
            ("outbound", "Outbound"),
            ("internal", "Internal"),
        ],
        string="Direction",
        required=True,
        default="outbound",
        index=True,
    )
    subject = fields.Char()
    sender = fields.Char()
    body_html = fields.Html(string="Message")
    source_key = fields.Char(copy=False, index=True)
    helpdesk_ticket_id = fields.Many2one(
        "helpdesk.ticket",
        string="Legal Collection Ticket",
        ondelete="set null",
        index=True,
    )
    mail_message_id = fields.Many2one(
        "mail.message",
        string="Odoo Email",
        ondelete="set null",
        index=True,
    )
    attachment_ids = fields.Many2many(
        "ir.attachment",
        "contract_litigation_communication_attachment_rel",
        "communication_id",
        "attachment_id",
        string="Attachments",
    )
    attachment_urls = fields.Text(string="External Attachments")
    is_unread = fields.Boolean(string="Unread", default=False, index=True)
    status = fields.Selection(
        selection_add=[("received", "Received")],
        ondelete={
            "received": lambda records: records.write({"status": "delivered"})
        },
    )

    _sql_constraints = [
        (
            "source_key_unique",
            "unique(source_key)",
            "This communication has already been recorded.",
        ),
    ]


class MailMessageLitigation(models.Model):
    _inherit = "mail.message"

    @api.model_create_multi
    def create(self, vals_list):
        messages = super().create(vals_list)
        if self.env.context.get("skip_litigation_message_capture"):
            return messages
        customer_subtype = self.env.ref("mail.mt_comment")
        for message in messages.filtered(
            lambda item: item.message_type == "email"
            or (
                item.message_type == "comment"
                and item.subtype_id == customer_subtype
            )
        ):
            try:
                with self.env.cr.savepoint():
                    message._capture_litigation_email()
            except Exception:
                _logger.exception(
                    "Could not add mail.message %s to litigation history",
                    message.id,
                )
        return messages

    def _capture_litigation_email(self):
        self.ensure_one()
        case = False
        ticket = False
        if self.model == "helpdesk.ticket" and self.res_id:
            ticket = self.env["helpdesk.ticket"].sudo().browse(
                self.res_id
            ).exists()
            case = ticket.litigation_case_id if ticket else False
        elif self.model == "contract.litigation.case" and self.res_id:
            case = self.env["contract.litigation.case"].sudo().browse(
                self.res_id
            ).exists()
            ticket = case._ensure_legal_helpdesk_ticket() if case else False
        if not case:
            return False

        source_key = "mail.message:%s" % self.id
        Communication = self.env["contract.litigation.communication"].sudo()
        existing = Communication.search([
            ("source_key", "=", source_key),
        ], limit=1)
        if existing:
            return existing
        internal_users = self.author_id.user_ids.filtered(
            lambda user: user.active and not user.share
        )
        direction = "outbound" if internal_users else "inbound"
        communication = Communication.create({
            "case_id": case.id,
            "communication_type": (
                "follow_up" if direction == "outbound" else "customer_reply"
            ),
            "channel": "email",
            "direction": direction,
            "sent_on": self.date or fields.Datetime.now(),
            "sender": self.email_from or self.author_id.email,
            "recipient": (
                case.contact_email
                if direction == "outbound"
                else "legal@cabal.sv"
            ) or case.partner_id.email or "Email",
            "status": "sent" if direction == "outbound" else "received",
            "user_id": (
                internal_users[:1].id
                or case.responsible_id.id
                or self.env.user.id
            ),
            "subject": self.subject,
            "body_html": self.body,
            "notes": self.subject or _("Email message"),
            "source_key": source_key,
            "helpdesk_ticket_id": ticket.id if ticket else False,
            "mail_message_id": self.id,
            "attachment_ids": [fields.Command.set(self.attachment_ids.ids)],
            "is_unread": direction == "inbound",
        })
        if (
            self.model == "contract.litigation.case"
            and direction == "inbound"
            and ticket
        ):
            ticket.with_context(
                skip_litigation_message_capture=True
            ).message_post(
                body=Markup("<p><strong>%s</strong></p>%s") % (
                    html_escape(_(
                        "Customer email received on the litigation case"
                    )),
                    self.body or Markup(""),
                ),
                attachment_ids=self.attachment_ids.ids,
                subtype_xmlid="mail.mt_note",
            )
        return communication


class MailMailLitigation(models.Model):
    _inherit = "mail.mail"

    @api.model_create_multi
    def create(self, vals_list):
        """Force every legal-ticket email through the approved Azure service."""
        prepared_values = []
        for original_values in vals_list:
            values = dict(original_values)
            model_name = values.get("model")
            res_id = values.get("res_id")
            message_id = values.get("mail_message_id")
            if message_id and (not model_name or not res_id):
                message = self.env["mail.message"].sudo().browse(
                    message_id
                ).exists()
                if message:
                    model_name = model_name or message.model
                    res_id = res_id or message.res_id
            if model_name == "helpdesk.ticket" and res_id:
                ticket = self.env["helpdesk.ticket"].sudo().browse(
                    res_id
                ).exists()
                if ticket and ticket.litigation_case_id:
                    mail_server = (
                        ticket.litigation_case_id._litigation_azure_mail_server()
                    )
                    values.update({
                        "email_from": LITIGATION_EMAIL_FROM,
                        "reply_to": LITIGATION_EMAIL_REPLY_TO,
                        "mail_server_id": mail_server.id,
                    })
            prepared_values.append(values)
        return super().create(prepared_values)
