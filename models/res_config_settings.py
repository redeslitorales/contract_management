# -*- coding: utf-8 -*-

import pytz

from odoo import fields, models

from .email_domain_utils import format_default_bad_email_domain_map


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    suspended_reservation_product_id = fields.Many2one(
        'product.product',
        string='Suspended Line Reservation Product',
        config_parameter='contract_management.suspended_reservation_product_id',
        domain="[('sale_ok', '=', True)]",
    )

    suspended_reservation_amount = fields.Float(
        string='Suspended Line Reservation Amount',
        config_parameter='contract_management.suspended_reservation_amount',
        default=7.0,
    )

    litigation_threshold_days = fields.Integer(
        string='Litigation Review Threshold (Days)',
        config_parameter='contract_management.litigation_threshold_days',
        default=90,
        help='Create a pre-litigation review case after this many suspended days.',
    )

    litigation_minimum_balance = fields.Float(
        string='Minimum Litigation Balance',
        digits=(16, 2),
        config_parameter='contract_management.litigation_minimum_balance',
        default=1.0,
        help='Do not create automated review cases below this open balance.',
    )

    litigation_response_days = fields.Integer(
        string='Notice Response Period (Days)',
        config_parameter='contract_management.litigation_response_days',
        default=10,
        help='Default response period used by initial and final notices.',
    )

    litigation_timezone = fields.Selection(
        selection=lambda self: [(timezone, timezone) for timezone in pytz.all_timezones],
        string='Collection Communication Time Zone',
        config_parameter='contract_management.litigation_timezone',
        default='America/El_Salvador',
        help='Time zone used to enforce the permitted collection-contact window.',
    )

    def action_submit_litigation_whatsapp_templates(self):
        return self.env['contract.litigation.case'].action_submit_litigation_whatsapp_templates()

    company_currency_id = fields.Many2one(
        related='company_id.currency_id',
        readonly=True,
    )

    docusign_company_signer_email = fields.Char(
        string='DocuSign Company Signer Email',
        help='Email address of the company representative who signs contracts after the customer',
        config_parameter='contract_management.docusign_company_signer_email'
    )
    
    docusign_company_stamp_base64 = fields.Char(
        string='DocuSign Company Stamp (Base64 PNG)',
        help='Base64-encoded PNG image of company stamp to appear on signed documents',
        config_parameter='contract_management.docusign_company_stamp_base64',
        size=None
    )

    docusign_service_user_id = fields.Many2one(
        'res.users',
        string='DocuSign Service User',
        help='Odoo user whose DocuSign tokens are used for contract envelopes (defaults to legacy contratos@cabal.sv).',
        config_parameter='contract_management.docusign_service_user_id',
    )
    
    contract_cancellation_email = fields.Char(
        string='Cancellation Notification Email',
        help='Email address to receive notifications when customers intend to cancel their contracts',
        config_parameter='contract_management.contract_cancellation_email'
    )

    contract_confirmation_secret = fields.Char(
        string='Quote Confirmation Secret',
        help='HMAC secret used to sign public quote confirmation links. Change to rotate links.',
        config_parameter='contract_management.confirm_secret',
    )

    wa_magic_template = fields.Char(
        string='Magic Link WhatsApp Template',
        help='Template name used to send DocuSign magic signing links over WhatsApp.',
        config_parameter='contract_management.wa_magic_template',
    )

    docusign_embedded_return_url = fields.Char(
        string='DocuSign Embedded Return URL',
        help='Optional absolute URL DocuSign should redirect to after embedded signing. If empty, the system builds one automatically.',
        config_parameter='contract_management.docusign_embedded_return_url',
    )

    wa_template_quote = fields.Char(
        string='Quote WhatsApp Template',
        help='Template name used to send quotes over WhatsApp (legacy provider).',
        config_parameter='wa_template_quote',
    )

    force_quote_email_only = fields.Boolean(
        string='Send Quotes via Email Only',
        help='Disable WhatsApp delivery for quotations and always send them by email.',
        config_parameter='contract_management.force_quote_email_only',
        default=True,
    )

    quote_confirm_skip_threshold = fields.Float(
        string='Quote Confirm Skip Threshold',
        help='Skip quote confirmation and auto-send contract when monthly payment is below this amount.',
        config_parameter='contract_management.quote_confirm_skip_threshold',
        default=100.0,
    )

    bad_email_domain_map_raw = fields.Char(
        string='Bad Email Domain Map',
        help='One mapping per line. Format: bad_domain -> correct_domain (e.g., gamil.com -> gmail.com).',
        config_parameter='contract_management.bad_email_domain_map',
        default=lambda self: format_default_bad_email_domain_map(),
    )
