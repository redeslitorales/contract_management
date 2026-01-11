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
import logging
from odoo.addons.odoo_docusign.models import docu_client

_logger = logging.getLogger(__name__)

SUBSCRIPTION_DRAFT_STATE = ['1_draft', '1a_pending', '1b_install', '1c_nocontract', '1d_internal', '1e_confirm', '2_renewal']
SUBSCRIPTION_ACTIVE_STATE = ['3_progress', '4_paused', '5_renewed']
SUBSCRIPTION_SUSPENDED_STATE = ['8_suspend']

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

STATE_FLOW = [
    'draft',
    'active',
    'renewal_due',
    'expired',
    'terminated',
]

ALLOWED_STATE_TRANSITIONS = {
    'draft': ['active'],
    'active': ['renewal_due', 'expired', 'terminated'],
    'renewal_due': ['active', 'expired', 'terminated'],
    'expired': ['terminated'],
    'terminated': [],
}

DOCUSIGN_LIVE = True

platform_type = {
    'dev': 'account-d.docusign.com',
    'prod': 'account.docusign.com'
}


class ContractManagement(models.Model):
    _name = 'contract.management'
    _description = 'Contract Management'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'portal.mixin']

    name = fields.Char(related="subscription_id.cabal_sequence", string='Contract Number', readonly=True)
    partner_id = fields.Many2one(related='subscription_id.partner_id', string='Customer', required=True)
    start_date = fields.Date(related="subscription_id.start_date", string='Start Date')
    end_date = fields.Date(string='End Date', compute='_compute_end_date', store=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('renewal_due', 'Renewal Due'),
        ('expired', 'Expired'),
        ('terminated', 'Terminated')
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
    has_signed_documents = fields.Boolean(string='Has Signed Documents', compute='_compute_has_signed_documents', store=False)
    early_termination_cost = fields.Float(string='Early Termination Cost', compute='_compute_early_termination_cost', store=False, digits=(16, 2), help="Contract value - Total paid + Early termination fee")

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

    def _compute_early_termination_cost(self):
        """Calculate early termination cost: contract_value - total_paid + early_termination_fee"""
        for contract in self:
            contract_value = contract.contract_value or 0.0
            total_paid = contract.total_paid or 0.0
            early_fee = contract.early_termination_fee or 0.0
            contract.early_termination_cost = contract_value - total_paid + early_fee

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
    
    @api.depends('signed_document_ids')
    def _compute_has_signed_documents(self):
        for contract in self:
            contract.has_signed_documents = bool(contract.signed_document_ids)
    
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

    def _allowed_next_states(self, current_state):
        """Return the allowed next states for the current state."""
        return ALLOWED_STATE_TRANSITIONS.get(current_state or 'draft', [])

    def _get_state_label(self, state_value):
        state_dict = dict(self._fields['state'].selection)
        return state_dict.get(state_value, state_value)

    def _validate_state_change(self, target_state):
        for contract in self:
            current_state = contract.state or 'draft'
            if target_state == current_state:
                continue
            allowed = contract._allowed_next_states(current_state)
            if target_state not in allowed:
                allowed_labels = ', '.join(contract._get_state_label(s) for s in allowed) or _('none')
                raise ValidationError(
                    _('Invalid state transition from %(current)s to %(target)s. Allowed next states: %(allowed)s') % {
                        'current': contract._get_state_label(current_state),
                        'target': contract._get_state_label(target_state),
                        'allowed': allowed_labels,
                    }
                )

    def write(self, vals):
        if 'state' in vals:
            target_state = vals.get('state')
            self._validate_state_change(target_state)
        return super().write(vals)

    def action_activate(self):
        for contract in self:
            if contract.state != 'active':
                # Validate transition before promoting to active
                contract._validate_state_change('active')
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
            if contract.state != 'terminated':
                contract._validate_state_change('terminated')
                contract.state = 'terminated'
            if contract.subscription_id:
                contract.subscription_id.action_cancel()
            if contract.early_termination_fee:
                # Logic to apply early termination fee
                pass

    @api.model
    def check_expired_contracts(self):
        today = date.today()
        expired_contracts = self.search([
            ('end_date', '<', today),
            ('state', 'in', ['active', 'renewal_due'])
        ])
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
    
    def _compute_access_url(self):
        """Compute portal URL for contract records."""
        super(ContractManagement, self)._compute_access_url()
        for contract in self:
            contract.access_url = '/my/contract/%s' % contract.id
    
    def _get_portal_return_action(self):
        """Return action for portal after viewing contract."""
        self.ensure_one()
        return '/my/services'
    
    def _get_docusign_headers(self, access_token):
        """Return headers for DocuSign API calls."""
        return {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }
    
    def _get_docusign_api_url(self, env):
        """Get DocuSign API base URL based on environment."""
        config = docu_client._get_docusign_config(env)
        return f"{config['base_uri']}/restapi/v2.1/accounts/{config['account_id']}"
    
    def _get_envelope_status(self, envelope_id):
        """Get current status of DocuSign envelope.
        
        Returns:
            str: Envelope status (created, sent, delivered, signed, completed, voided, etc.)
        """
        self.ensure_one()
        
        _logger.info("[DocuSign] Getting status for envelope %s", envelope_id)
        
        try:
            # Get access token
            user = self.env['res.users'].browse(196)
            access_token = docu_client._get_cached_access_token(self.env, user)
            
            # Build API URL
            api_base = self._get_docusign_api_url(self.env)
            url = f"{api_base}/envelopes/{envelope_id}"
            
            # Make GET request
            response = requests.get(
                url,
                headers=self._get_docusign_headers(access_token)
            )
            
            if response.status_code != 200:
                _logger.error("[DocuSign] Failed to get envelope status: %s", response.text)
                return None
            
            result = response.json()
            status = result.get('status')
            _logger.info("[DocuSign] Envelope %s status: %s", envelope_id, status)
            
            return status
            
        except Exception as e:
            _logger.exception("[DocuSign] Error getting envelope status: %s", str(e))
            return None
    
    def _update_envelope_recipient(self, envelope_id, recipient_id, new_email=None, new_phone=None, resend_envelope=False):
        """Update DocuSign envelope recipient information.
        
        Args:
            envelope_id: DocuSign envelope ID
            recipient_id: Recipient ID within envelope
            new_email: New email address (optional)
            new_phone: New phone number (optional)
            resend_envelope: If True, DocuSign will resend notification after update
        """
        self.ensure_one()
        
        try:
            # Get access token
            user = self.env['res.users'].browse(196)  # contratos@cabal.sv
            access_token = docu_client._get_cached_access_token(self.env, user)
            
            # Build API URL with resend_envelope parameter
            api_base = self._get_docusign_api_url(self.env)
            url = f"{api_base}/envelopes/{envelope_id}/recipients"
            if resend_envelope:
                url += "?resend_envelope=true"
            
            # Prepare recipient update payload
            recipient_update = {
                'signers': [{
                    'recipientId': recipient_id,
                }]
            }
            
            if new_email:
                # Switching to email delivery (default DocuSign method)
                recipient_update['signers'][0]['email'] = new_email
                # Remove SMS delivery method if previously set
                recipient_update['signers'][0]['deliveryMethod'] = None
            elif new_phone:
                # Switching to SMS delivery
                # Parse phone number to extract country code and number
                # Expected format: +503XXXXXXXX or +1XXXXXXXXXX
                phone_cleaned = new_phone.lstrip('+')
                
                # Determine country code (503 for El Salvador, 1 for USA, etc.)
                if phone_cleaned.startswith('503'):
                    country_code = '503'
                    number = phone_cleaned[3:]
                elif phone_cleaned.startswith('1') and len(phone_cleaned) == 11:
                    country_code = '1'
                    number = phone_cleaned[1:]
                elif phone_cleaned.startswith('56'):  # Chile
                    country_code = '56'
                    number = phone_cleaned[2:]
                else:
                    # Fallback: assume first 1-3 digits are country code
                    country_code = phone_cleaned[:3]
                    number = phone_cleaned[3:]
                
                # Include email for recipient identity, but deliver via WhatsApp
                if self.partner_id.email:
                    recipient_update['signers'][0]['email'] = self.partner_id.email
                
                recipient_update['signers'][0]['deliveryMethod'] = 'WhatsApp'
                recipient_update['signers'][0]['phoneNumber'] = {
                    'countryCode': country_code,
                    'number': number
                }
                
                _logger.info("[DocuSign] WhatsApp delivery: email=%s, phone=+%s%s", 
                           self.partner_id.email, country_code, number)
            
            # Make PUT request to update recipient
            response = requests.put(
                url,
                headers=self._get_docusign_headers(access_token),
                json=recipient_update
            )
            
            if response.status_code not in [200, 201]:
                _logger.error("[DocuSign] Failed to update recipient: %s", response.text)
                raise ValidationError(_(f"Failed to update recipient: {response.text}"))
            
            _logger.info("[DocuSign] Successfully updated recipient %s on envelope %s", recipient_id, envelope_id)
            return True
            
        except Exception as e:
            _logger.exception("[DocuSign] Error updating envelope recipient: %s", str(e))
            raise ValidationError(_(f"Error updating envelope recipient: {str(e)}"))
    
    def _send_envelope_notification(self, envelope_id, recipient_id):
        """Send DocuSign notification to recipient (for unsigned envelopes after updating)."""
        self.ensure_one()
        
        _logger.info("[DocuSign] _send_envelope_notification called for envelope %s, recipient %s", envelope_id, recipient_id)
        
        try:
            # Get access token
            user = self.env['res.users'].browse(196)  # contratos@cabal.sv
            _logger.info("[DocuSign] Getting access token for user %s", user.login)
            access_token = docu_client._get_cached_access_token(self.env, user)
            _logger.info("[DocuSign] Access token obtained: %s...", access_token[:20] if access_token else 'None')
            
            # Build API URL for sending notification
            api_base = self._get_docusign_api_url(self.env)
            url = f"{api_base}/envelopes/{envelope_id}/notification"
            _logger.info("[DocuSign] Sending notification to URL: %s", url)
            
            # Make PUT request with recipient details to send notification
            payload = {
                "recipients": {
                    "signers": [{
                        "recipientId": recipient_id
                    }]
                }
            }
            _logger.info("[DocuSign] Payload: %s", payload)
            
            response = requests.put(
                url,
                headers=self._get_docusign_headers(access_token),
                json=payload
            )
            
            _logger.info("[DocuSign] Response status: %s", response.status_code)
            _logger.info("[DocuSign] Response body: %s", response.text)
            
            if response.status_code not in [200, 201]:
                _logger.error("[DocuSign] Failed to send notification: %s - %s", response.status_code, response.text)
                raise ValidationError(_(f"Failed to send notification: {response.text}"))
            
            _logger.info("[DocuSign] Successfully sent notification to recipient %s on envelope %s", recipient_id, envelope_id)
            return True
            
        except Exception as e:
            _logger.exception("[DocuSign] Error sending envelope notification: %s", str(e))
            raise ValidationError(_(f"Error sending envelope notification: {str(e)}"))
    
    def _resend_envelope_notification(self, envelope_id, recipient_id):
        """Resend DocuSign notification to recipient (for signed envelopes only)."""
        self.ensure_one()
        
        _logger.info("[DocuSign] _resend_envelope_notification called for envelope %s, recipient %s", envelope_id, recipient_id)
        
        try:
            # Get access token
            user = self.env['res.users'].browse(196)  # contratos@cabal.sv
            _logger.info("[DocuSign] Getting access token for user %s", user.login)
            access_token = docu_client._get_cached_access_token(self.env, user)
            _logger.info("[DocuSign] Access token obtained: %s...", access_token[:20] if access_token else 'None')
            
            # Build API URL for resend notification
            api_base = self._get_docusign_api_url(self.env)
            url = f"{api_base}/envelopes/{envelope_id}/recipients/{recipient_id}/resend_envelope"
            _logger.info("[DocuSign] Resending notification to URL: %s", url)
            
            # Make PUT request to resend notification (DocuSign uses PUT with empty body)
            response = requests.put(
                url,
                headers=self._get_docusign_headers(access_token),
                json={}
            )
            
            _logger.info("[DocuSign] Response status: %s", response.status_code)
            _logger.info("[DocuSign] Response body: %s", response.text)
            
            if response.status_code not in [200, 201]:
                _logger.error("[DocuSign] Failed to resend notification: %s", response.text)
                raise ValidationError(_(f"Failed to resend notification: {response.text}"))
            
            _logger.info("[DocuSign] Successfully resent notification to recipient %s on envelope %s", recipient_id, envelope_id)
            return True
            
        except Exception as e:
            _logger.exception("[DocuSign] Error resending envelope notification: %s", str(e))
            raise ValidationError(_(f"Error resending envelope notification: {str(e)}"))
    
    def action_resend_via_whatsapp(self):
        """Resend DocuSign envelope via WhatsApp - updates contact if not signed, resends if signed."""
        self.ensure_one()
        
        _logger.info("[DocuSign] action_resend_via_whatsapp called for contract %s", self.id)
        _logger.info("[DocuSign] Partner: %s, WhatsApp: %s", self.partner_id.name, self.partner_id.whatsapp)
        
        if not self.docusign_id:
            raise UserError(_("No DocuSign envelope found for this contract."))
        
        if not self.partner_id.whatsapp:
            raise UserError(_("Customer does not have a WhatsApp number configured."))
        
        # Validate WhatsApp format
        match = re.match(r'^\+(\d{1,3})(\d+)$', self.partner_id.whatsapp)
        if not match:
            raise UserError(_("Customer WhatsApp number is not in valid format (+country_code phone_number)."))
        
        # Get first connector line (customer signer)
        customer_line = self.docusign_id.connector_line_ids.filtered(
            lambda l: l.partner_id.id == self.partner_id.id
        )[:1]
        
        _logger.info("[DocuSign] Found %d connector lines for partner", len(customer_line))
        
        if not customer_line:
            raise UserError(_("No customer signer found in DocuSign envelope."))
        
        if not customer_line.envelope_id:
            raise UserError(_("No envelope ID found. Cannot resend."))
        
        # Check if customer has already signed (sign_status is Boolean)
        customer_signed = customer_line.sign_status == True
        
        envelope_id = customer_line.envelope_id
        recipient_id = customer_line.recipient_id or '1'  # Default to '1' if not set
        
        _logger.info("[DocuSign] Envelope ID: %s, Recipient ID: %s, Customer signed: %s", envelope_id, recipient_id, customer_signed)
        
        try:
            # Get envelope status to determine how to proceed
            envelope_status = self._get_envelope_status(envelope_id)
            _logger.info("[DocuSign] Envelope status: %s", envelope_status)
            
            if not envelope_status:
                raise UserError(_("Failed to get envelope status from DocuSign"))
            
            if envelope_status in ['voided', 'declined']:
                # Envelope was voided or declined - create and send new envelope
                _logger.info("[DocuSign] Envelope status is '%s' - creating new envelope", envelope_status)
                if not self.subscription_id:
                    raise UserError(_(f"Cannot create new envelope: No subscription linked to this contract."))
                
                # Call subscription's send_docs to create new envelope
                try:
                    self.subscription_id.send_docs()
                    # Update send method after successful creation
                    self.subscription_id.write({'contract_send_method': 'whatsapp'})
                    msg = f"Previous envelope was {envelope_status}. New DocuSign envelope created and sent via WhatsApp to {self.partner_id.whatsapp}."
                except Exception as create_error:
                    _logger.error("[DocuSign] Failed to create new envelope: %s", str(create_error))
                    raise UserError(_(f"Failed to create new envelope: {str(create_error)}"))
            elif envelope_status == 'completed':
                # Envelope is completed - just resend notification (reminder of signed document)
                _logger.info("[DocuSign] Envelope status is 'completed' - resending notification")
                self._resend_envelope_notification(envelope_id, recipient_id)
                msg = f"DocuSign notification resent for completed envelope (reminder sent via WhatsApp)."
            elif envelope_status == 'created':
                # Envelope was created but never sent - send it now
                _logger.info("[DocuSign] Envelope status is 'created' - sending envelope")
                # Update delivery method to WhatsApp and send
                self._update_envelope_recipient(
                    envelope_id,
                    recipient_id,
                    new_phone=self.partner_id.whatsapp,
                    resend_envelope=True
                )
                msg = f"DocuSign envelope sent via WhatsApp to {self.partner_id.whatsapp}."
            elif envelope_status == 'sent':
                # Envelope was sent - update recipient and resend
                _logger.info("[DocuSign] Envelope status is 'sent' - updating and resending")
                self._update_envelope_recipient(
                    envelope_id,
                    recipient_id,
                    new_phone=self.partner_id.whatsapp,
                    resend_envelope=True
                )
                msg = f"DocuSign notification resent via WhatsApp to {self.partner_id.whatsapp}."
            else:
                # Other statuses (delivered, signed, etc.)
                _logger.info("[DocuSign] Envelope status is '%s' - resending notification", envelope_status)
                self._resend_envelope_notification(envelope_id, recipient_id)
                msg = f"DocuSign notification resent (envelope status: {envelope_status})."
            
            # Update send method
            self.write({'contract_send_method': 'whatsapp'})
            
            # Log to chatter
            self.message_post(
                body=msg,
                subject="DocuSign Resent via WhatsApp"
            )
            
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Success'),
                    'message': msg,
                    'type': 'success',
                    'sticky': False,
                }
            }
            
        except Exception as e:
            error_msg = str(e)
            self.message_post(
                body=f"Failed to resend via WhatsApp: {error_msg}",
                subject="DocuSign Resend Failed"
            )
            raise
    
    def action_resend_via_email(self):
        """Resend DocuSign envelope via Email - updates contact if not signed, resends if signed."""
        self.ensure_one()
        
        _logger.info("[DocuSign] action_resend_via_email called for contract %s", self.id)
        _logger.info("[DocuSign] Partner: %s, Email: %s", self.partner_id.name, self.partner_id.email)
        
        if not self.docusign_id:
            raise UserError(_("No DocuSign envelope found for this contract."))
        
        if not self.partner_id.email:
            raise UserError(_("Customer does not have an email address configured."))
        
        # Get first connector line (customer signer)
        customer_line = self.docusign_id.connector_line_ids.filtered(
            lambda l: l.partner_id.id == self.partner_id.id
        )[:1]
        
        _logger.info("[DocuSign] Found %d connector lines for partner", len(customer_line))
        
        if not customer_line:
            raise UserError(_("No customer signer found in DocuSign envelope."))
        
        if not customer_line.envelope_id:
            raise UserError(_("No envelope ID found. Cannot resend."))
        
        # Check if customer has already signed (sign_status is Boolean)
        customer_signed = customer_line.sign_status == True
        
        envelope_id = customer_line.envelope_id
        recipient_id = customer_line.recipient_id or '1'  # Default to '1' if not set
        
        _logger.info("[DocuSign] Envelope ID: %s, Recipient ID: %s, Customer signed: %s", envelope_id, recipient_id, customer_signed)
        
        try:
            # Get envelope status to determine how to proceed
            envelope_status = self._get_envelope_status(envelope_id)
            _logger.info("[DocuSign] Envelope status: %s", envelope_status)
            
            if not envelope_status:
                raise UserError(_("Failed to get envelope status from DocuSign"))
            
            if envelope_status in ['voided', 'declined']:
                # Envelope was voided or declined - create and send new envelope
                _logger.info("[DocuSign] Envelope status is '%s' - creating new envelope", envelope_status)
                if not self.subscription_id:
                    raise UserError(_(f"Cannot create new envelope: No subscription linked to this contract."))
                
                # Call subscription's send_docs to create new envelope
                try:
                    self.subscription_id.send_docs()
                    # Update send method after successful creation
                    self.subscription_id.write({'contract_send_method': 'email'})
                    msg = f"Previous envelope was {envelope_status}. New DocuSign envelope created and sent via Email to {self.partner_id.email}."
                except Exception as create_error:
                    _logger.error("[DocuSign] Failed to create new envelope: %s", str(create_error))
                    raise UserError(_(f"Failed to create new envelope: {str(create_error)}"))
            elif envelope_status == 'completed':
                # Envelope is completed - just resend notification (reminder of signed document)
                _logger.info("[DocuSign] Envelope status is 'completed' - resending notification")
                self._resend_envelope_notification(envelope_id, recipient_id)
                msg = f"DocuSign notification resent for completed envelope (reminder sent via Email)."
            elif envelope_status == 'created':
                # Envelope was created but never sent - send it now
                _logger.info("[DocuSign] Envelope status is 'created' - sending envelope")
                # Update delivery method to email and send
                self._update_envelope_recipient(
                    envelope_id,
                    recipient_id,
                    new_email=self.partner_id.email,
                    resend_envelope=True
                )
                msg = f"DocuSign envelope sent via Email to {self.partner_id.email}."
            elif envelope_status == 'sent':
                # Envelope was sent - update recipient and resend
                _logger.info("[DocuSign] Envelope status is 'sent' - updating and resending")
                self._update_envelope_recipient(
                    envelope_id,
                    recipient_id,
                    new_email=self.partner_id.email,
                    resend_envelope=True
                )
                msg = f"DocuSign notification resent via Email to {self.partner_id.email}."
            else:
                # Other statuses (delivered, signed, etc.)
                _logger.info("[DocuSign] Envelope status is '%s' - resending notification", envelope_status)
                self._resend_envelope_notification(envelope_id, recipient_id)
                msg = f"DocuSign notification resent (envelope status: {envelope_status})."
            
            # Update send method
            self.write({'contract_send_method': 'email'})
            
            # Log to chatter
            self.message_post(
                body=msg,
                subject="DocuSign Resent via Email"
            )
            
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Success'),
                    'message': msg,
                    'type': 'success',
                    'sticky': False,
                }
            }
            
        except Exception as e:
            error_msg = str(e)
            self.message_post(
                body=f"Failed to resend via Email: {error_msg}",
                subject="DocuSign Resend Failed"
            )
            raise

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
                        if subscription.subscription_state == '1a_pending':  # Was awaiting customer signature
                            subscription.subscription_state = '1d_internal'  # Customer signed, awaiting Cabal signature
                        else:
                            # Post warning to subscription chatter
                            subscription.message_post(
                                body=_("Could not update subscription state. Current state is '%s' but should have been '1a_pending' (Pending Signature) for recipient-completed event.") % dict(SUBSCRIPTION_STATES).get(subscription.subscription_state, subscription.subscription_state),
                                subject=_('DocuSign State Mismatch Warning'),
                                message_type='notification',
                                subtype_xmlid='mail.mt_note'
                            )
                            _logger.warning("[DocuSign Webhook] State mismatch for subscription %s: current=%s, expected=1a_pending", subscription.id, subscription.subscription_state)
                    if event == 'envelope-completed':
                        connector.state = 'completed'
                        subscription = request.env['sale.order'].sudo().browse(connector.sale_id.id)
                        if subscription.subscription_state == '1d_internal':  # Customer signed, awaiting Cabal signature
                            subscription.subscription_state = '1b_install'  # All signatures complete, ready for install
                        else:
                            # Post warning to subscription chatter
                            subscription.message_post(
                                body=_("Could not update subscription state. Current state is '%s' but should have been '1d_internal' (Pending Cabal Signature) for envelope-completed event.") % dict(SUBSCRIPTION_STATES).get(subscription.subscription_state, subscription.subscription_state),
                                subject=_('DocuSign State Mismatch Warning'),
                                message_type='notification',
                                subtype_xmlid='mail.mt_note'
                            )
                            _logger.warning("[DocuSign Webhook] State mismatch for subscription %s: current=%s, expected=1d_internal", subscription.id, subscription.subscription_state)
                        
                        # Auto-download signed documents (skip if already present to avoid duplicates)
                        try:
                            if not any(connector.connector_line_ids.mapped('signed_attachment_ids')):
                                connector.download_docs()
                            else:
                                _logger.info("[DocuSign Webhook] Signed attachments already present; skipping download for envelope %s", envelope_id)
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
                
    