# -*- coding: utf-8 -*-

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

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
