from odoo import models, fields, api, http, _
from odoo.http import request
from odoo.exceptions import UserError, ValidationError
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
import time
import base64
import re
import json
import jwt
import requests
from odoo.addons.odoo_docusign.models import docu_client

SUBSCRIPTION_DRAFT_STATE = ['1_draft', '1a_pending', '1b_install', '1c_nocontract', '1d_internal', '1e_confirm', '2_renewal']

SUBSCRIPTION_STATES = [
    ('1_draft', 'Quotation'),  # Quotation for a new subscription
    ('1a_pending', 'Pending Signature'),  # Confirmed subscription waiting for a signature
    ('1b_install', 'Pending Install'),  # Confirmed subscription waiting for an installation
    ('1c_nocontract', 'Pending Contract'),  # Confirmed subscription waiting for a contract to be generated
    ('1d_internal', 'Pending Cabal Signature'),  # Confirmed subscription waiting for Cabal to sign
    ('1e_confirm', 'Quotation Confirmed'),  # Quotation has been confirmed by client.  Waiting for contract to be generated.
    ('2_renewal', 'Renewal Quotation'),  # Renewal Quotation for existing subscription
    ('3_progress', 'In Progress'),  # Active Subscription or confirmed renewal for active subscription
    ('4_paused', 'Paused'),  # Active subscription with paused invoicing
    ('5_renewed', 'Renewed'),  # Active or ended subscription that has been renewed
    ('6_churn', 'Churned'),  # Closed or ended subscription
    ('7_upsell', 'Upsell'),  # Quotation or SO upselling a subscription
    ('8_suspend', 'Suspended'),  # Suspended
]

CONTRACT_SEND_METHODS = [
        ('whatsapp', 'WhatsApp'),
        ('email', 'Email'),
        ('physical', 'Physical'),
        ('donotsend', 'Do Not Send')
#        ('sms', 'SMS')
]

DOCUSIGN_LIVE = True

platform_type = {
    'dev': 'account-d.docusign.com',
    'prod': 'account.docusign.com'
}


class ContractManagement(models.Model):
    _name = 'contract.management'
    _description = 'Contract Management'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(related="subscription_id.cabal_sequence", string='Contract Number', readonly=True)
    partner_id = fields.Many2one(related='subscription_id.partner_id', string='Customer', required=True)
    start_date = fields.Date(related="subscription_id.start_date", string='Start Date')
    end_date = fields.Date(string='End Date', compute='_compute_end_date', store=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('expired', 'Expired'),
        ('terminated', 'Terminated'),
        ('renewal_due', 'Renewal Due'),
        ('signature_in_process', 'Signature In Process'),
        ('signed', 'Signed')
    ], string='Status', default='draft', tracking=True)
    service_ids = fields.One2many('contract.service', 'contract_id', string='Services')
    total_paid = fields.Float(string='Total Paid', compute='_compute_total_paid', store=False, help="Sum of paid monthly invoices (tax-inclusive) linked to this subscription")
    subscription_id = fields.Many2one('sale.order', string='Subscription')
    contract_template = fields.Many2one(related='subscription_id.contract_template', string='Contract Template')
    docusign_id = fields.Many2one('docusign.connector', string='Docusign Record')
    docusign_status = fields.Selection(related='docusign_id.state', string="Signature Status",store=True)
    contract_send_method = fields.Selection(string='Send Method', selection=CONTRACT_SEND_METHODS, default="whatsapp")
    early_termination_fee = fields.Float(string='Early Termination Fee')
    late_charge = fields.Float(string='Late Charge')
    service_pause_count = fields.Integer(string='Number of Service Pauses', default=0)
    max_service_pause_duration = fields.Integer(string='Maximum Duration of Service Pauses (days)', default=0)
    contract_term = fields.Many2one(related='subscription_id.contract_term', string='Contract Term')
    clause_ids = fields.Many2many('contract.clause', string='Clauses')
    contract_file = fields.Binary(string='Contract File')
    contract_filename = fields.Char(string='Contract Filename')
    signed_document_ids = fields.Many2many(
        'ir.attachment',
        relation='contract_management_signed_document_rel',
        column1='contract_id',
        column2='attachment_id',
        string='Signed Documents',
        compute='_compute_signed_documents',
        store=False
    )
    document_count = fields.Integer(string='Document Count', compute='_compute_document_count')
    monthly_payment = fields.Float(string='Monthly Payment', digits=(16, 2), help="Monthly payment amount from DocuSign envelope")
    contract_value = fields.Float(string='Contract Value', digits=(16, 2), help="Total contract value from DocuSign envelope")

    def _compute_total_paid(self):
        for contract in self:
            total = 0.0
            subscription = contract.subscription_id
            if subscription:
                # Consider posted customer invoices that are fully paid
                invoices = subscription.invoice_ids.filtered(
                    lambda inv: getattr(inv, 'move_type', 'out_invoice') == 'out_invoice'
                    and inv.state == 'posted'
                    and inv.payment_state == 'paid'
                )
                for inv in invoices:
                    # Prefer summing only recurring lines; fallback to whole invoice if not detectable
                    recurring_lines = inv.invoice_line_ids.filtered(
                        lambda l: any(sl.product_id.recurring_invoice for sl in l.sale_line_ids)
                    )
                    if recurring_lines:
                        total += sum(recurring_lines.mapped('price_total'))
                    else:
                        total += inv.amount_total
            contract.total_paid = total

    def action_recompute_total_paid(self):
        """Manually recompute the non-stored field `total_paid` and refresh the view.
        Useful when invoice/payment state changes and a quick UI refresh is desired.
        """
        # Re-run the compute on current records
        self._compute_total_paid()
        # Post a small note per record for auditability
        for contract in self:
            contract.message_post(body=_('Total Paid recomputed: %0.2f') % (contract.total_paid or 0.0))
        # Reload the form/list to reflect the recomputed value
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }

    @api.depends('start_date', 'contract_term')
    def _compute_end_date(self):
        for contract in self:
            if contract.start_date and contract.contract_term:
                contract.end_date = contract.start_date + relativedelta(months=contract.contract_term.term)
            else:
                contract.end_date = False
    
    @api.depends('docusign_id', 'docusign_id.connector_line_ids.signed_attachment_ids')
    def _compute_signed_documents(self):
        """Get all signed documents from related DocuSign envelopes"""
        for contract in self:
            if contract.docusign_id:
                # Get all signed attachments from all recipients
                signed_attachments = contract.docusign_id.connector_line_ids.mapped('signed_attachment_ids')
                contract.signed_document_ids = signed_attachments
            else:
                contract.signed_document_ids = False
    
    @api.depends('signed_document_ids')
    def _compute_document_count(self):
        for contract in self:
            contract.document_count = len(contract.signed_document_ids)
    
    def action_view_documents(self):
        """Smart button action to view signed documents"""
        self.ensure_one()
        return {
            'name': _('Signed Documents'),
            'type': 'ir.actions.act_window',
            'res_model': 'ir.attachment',
            'view_mode': 'kanban,tree,form',
            'domain': [('id', 'in', self.signed_document_ids.ids)],
            'context': {'create': False}
        }
    
    def action_view_docusign(self):
        """Open the related DocuSign envelope"""
        self.ensure_one()
        if not self.docusign_id:
            raise ValidationError(_('No DocuSign envelope associated with this contract.'))
        return {
            'name': _('DocuSign Envelope'),
            'type': 'ir.actions.act_window',
            'res_model': 'docusign.connector',
            'view_mode': 'form',
            'res_id': self.docusign_id.id,
            'target': 'current'
        }

    def action_activate(self):
        for contract in self:
            contract.state = 'active'
            if not contract.subscription_id:
                subscription = self.env['sale.order'].create({
                    'name': contract.name,
                    'partner_id': contract.partner_id.id,
                    'order_line': [(0, 0, {
                        'name': service.name,
                        'product_id': service.product_id.id,
                        'product_uom_qty': 1,
                        'price_unit': service.price,
                    }) for service in contract.service_ids]
                })
                contract.subscription_id = subscription

    def action_terminate(self):
        for contract in self:
            contract.state = 'terminated'
            if contract.subscription_id:
                contract.subscription_id.action_cancel()
            if contract.early_termination_fee:
                # Logic to apply early termination fee
                pass

    @api.model
    def check_expired_contracts(self):
        today = date.today()
        expired_contracts = self.search([('end_date', '<', today), ('state', '=', 'active')])
        for contract in expired_contracts:
            contract.state = 'expired'
            if contract.subscription_id:
                contract.subscription_id.action_cancel()
            if contract.late_charge:
                # Logic to apply late charge
                pass

    @api.model
    def check_renewal_due_contracts(self):
        today = date.today()
        renewal_due_date = today + timedelta(days=180)
        renewal_due_contracts = self.search([('end_date', '<=', renewal_due_date), ('state', '=', 'active')])
        for contract in renewal_due_contracts:
            contract.state = 'renewal_due'

class ContractService(models.Model):
    _name = 'contract.service'
    _description = 'Contract Service'

    name = fields.Char(string='Service Name', required=True)
    price = fields.Float(string='Price', required=True)
    contract_id = fields.Many2one('contract.management', string='Contract')
    product_id = fields.Many2one('product.product', string='Product', required=True)

class ContractClause(models.Model):
    _name = 'contract.clause'
    _description = 'Contract Clause'
    _order = "sequence, id"

    active = fields.Boolean(string='Active', required=True, default=True)
    inactive_date = fields.Datetime(string='Inactive Date')
    name = fields.Char(string='Clause Name', required=True, translate=True)
    clause = fields.Text(string='Clause Language', translate=True)
    friendly_clause = fields.Text(string='Friendly Clause Language', translate=True)
    contract_template_ids = fields.Many2many('ir.actions.report', string='Applicable Contract Templates', domain=[('name', 'ilike', 'Contract')])
    sequence = fields.Integer(string='Sequence', default=10)
    version = fields.Integer(string='Version', default=1)

    @api.model
    def create(self, vals):
        # Get the existing records with the same name
        existing_clauses = self.search([('name', '=', vals.get('name'))])
        if existing_clauses:
            # Set the version to the next version number
            vals['version'] = max(existing_clauses.mapped('version')) + 1
        return super(ContractClause, self).create(vals)

    @api.model
    def get_applicable_clauses(self, contract_template_id):
        return self.search([
            ('contract_template_ids', 'in', contract_template_id),
            ('active', '=', True)
        ])
    
class DocuSignWebhookController(http.Controller):

    @http.route('/docusign/webhook', type='json', auth='public', methods=['POST'], csrf=False)
    def docusign_webhook(self, **kwargs):
        # Get the JSON data from the webhook
        data = json.loads(request.httprequest.data)
        
        # Extract the event and envelope ID
        event = data.get('event')
        envelope_id = data.get('data', {}).get('envelopeId')
        current_user = request.env.user

        if event and envelope_id:
            # Find the corresponding record in docusign.connector
            docusign_connector_line = request.env['docusign.connector.lines'].sudo().search([('envelope_id', '=', envelope_id)], limit=1)
            if docusign_connector_line:
                docusign_connector = request.env['docusign.connector'].sudo().browse(docusign_connector_line.record_id.id)
            if docusign_connector:
                pt = 'dev'
                if not 'test' in request.httprequest.url_root:
                    pt = 'prod'

                # Create the JWT assertion
                now = int(time.time())
                payload = {
                    'iss': request.env['ir.config_parameter'].sudo().get_param('docusign_client_id', ''),
                    'sub': request.env['ir.config_parameter'].sudo().get_param('docusign_user_id', ''),
                    'aud': platform_type[pt],
                    'iat': now,
                    'exp': now + 3600,
                    'scope': 'signature impersonation'
                }
                jwt_assertion = jwt.encode(payload, request.env['ir.config_parameter'].sudo().get_param('docusign_private_key', ''), algorithm='RS256')
                # Request an access token - default to dev environment unless the request URL does not contain the word test
                url = "https://{0}/oauth/token".format(platform_type[pt])
                headers = {
                    'Content-Type': 'application/x-www-form-urlencoded'
                }
                data = {
                    'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
                    'assertion': jwt_assertion
                }
                response = requests.post(url, headers=headers, data=data)
                access_token = response.json().get('access_token')

                if not access_token:
                    raise ValidationError(_("Failed to obtain access token from DocuSign"))

                # Trigger the method from docu_client.py using JWT authentication
                for connector in docusign_connector:
                    if event == 'recipient-completed':
                        connector.state = 'customer'
                        subscription = request.env['sale.order'].sudo().browse(connector.sale_id.id)
                        subscription.subscription_state = '1d_internal'  # Customer signed, awaiting Cabal signature
                    if event == 'envelope-completed':
                        connector.state = 'completed'
                        subscription = request.env['sale.order'].sudo().browse(connector.sale_id.id)
                        subscription.subscription_state = '1b_install'  # All signatures complete, ready for install
                        
                        # Auto-download signed documents
                        try:
                            connector.download_docs()
                        except Exception as e:
                            _logger.error("[DocuSign Webhook] Failed to auto-download documents: %s", str(e))
                    if connector.state != 'completed':
                        connector.status_docs()
        
        return {'status': 'success'}
    
class SaleOrderWebhook(http.Controller):

    @http.route('/webhook/confirm_sale_order', type='http', auth='public', methods=['GET'])

    def cnfirm_sale_order(self, uuid=None, send_method=None):
        if not send_method:
            send_method = 'whatsapp'
        if not uuid:
            return request.redirect('/quote_reject')
        else:
            sale_order = request.env['sale.order'].sudo().search([('confirmation_uuid', '=', uuid)], limit=1)
            if sale_order and sale_order.state in ['draft', 'sent']:
                sale_order.write({'quote_confirmed': True, 'subscription_state': '1e_confirm', 'contract_send_method': send_method, 'tag_ids': [(4, 2)]})
                return request.redirect('/quote_confirmed')
                # Automation rule 'Process Confirmed Quote' will do the processing of the confirmation
            else:
                return request.redirect('/quote_reject')
                
    