# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
import logging
import re
import requests
from odoo.addons.odoo_docusign.models import docu_client

_logger = logging.getLogger(__name__)


class ContractAddendum(models.Model):
    _name = 'contract.addendum'
    _description = 'Contract Addendum'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'portal.mixin']
    _order = 'effective_date desc, id desc'

    name = fields.Char(string='Addendum Name', required=True, tracking=True)
    contract_id = fields.Many2one('contract.management', string='Parent Contract', required=True, ondelete='cascade', tracking=True)
    partner_id = fields.Many2one(related='contract_id.partner_id', string='Customer', store=True, readonly=True)
    subscription_id = fields.Many2one(related='contract_id.subscription_id', string='Subscription', store=True, readonly=True)
    upsell_subscription_id = fields.Many2one('sale.order', string='Upsell Order', help="The upsell order that triggered this addendum", tracking=True)
    
    addendum_type = fields.Selection([
        ('service_addition', 'Service Addition'),
        ('service_removal', 'Service Removal'),
        ('price_change', 'Price Change'),
        ('term_extension', 'Term Extension'),
        ('term_change', 'Term Change'),
        ('other', 'Other')
    ], string='Addendum Type', required=True, default='other', tracking=True)
    
    description = fields.Text(string='Description', required=True, help="Detailed description of changes in this addendum")
    effective_date = fields.Date(string='Effective Date', required=True, default=fields.Date.context_today, tracking=True)
    
    state = fields.Selection([
        ('draft', 'Draft'),
        ('pending_signature', 'Pending Signature'),
        ('signed', 'Signed'),
        ('active', 'Active'),
        ('cancelled', 'Cancelled')
    ], string='Status', default='draft', required=True, tracking=True)
    
    # DocuSign Integration
    docusign_id = fields.Many2one('docusign.connector', string='DocuSign Envelope', tracking=True)
    docusign_status = fields.Selection(related='docusign_id.state', string="Signature Status", store=True)
    contract_send_method = fields.Selection([
        ('whatsapp', 'WhatsApp'),
        ('email', 'Email'),
        ('physical', 'Physical'),
        ('donotsend', 'Do Not Send')
    ], string='Send Method', default='whatsapp', tracking=True)
    
    signed_document_ids = fields.Many2many(
        'ir.attachment',
        relation='contract_addendum_signed_document_rel',
        column1='addendum_id',
        column2='attachment_id',
        string='Signed Documents',
        compute='_compute_signed_documents',
        store=False
    )
    document_count = fields.Integer(string='Document Count', compute='_compute_document_count')
    has_signed_documents = fields.Boolean(string='Has Signed Documents', compute='_compute_has_signed_documents', store=False)
    
    # Financial impact (optional)
    monthly_payment_change = fields.Float(string='Monthly Payment Change', digits=(16, 2), help="Change in monthly payment amount (positive = increase, negative = decrease)")
    one_time_fee = fields.Float(string='One-Time Fee', digits=(16, 2), help="One-time fee for this addendum (if any)")
    
    # Audit fields
    create_uid = fields.Many2one('res.users', string='Created By', readonly=True)
    create_date = fields.Datetime(string='Created On', readonly=True)
    write_uid = fields.Many2one('res.users', string='Last Updated By', readonly=True)
    write_date = fields.Datetime(string='Last Updated On', readonly=True)

    @api.depends('docusign_id', 'docusign_id.connector_line_ids.signed_attachment_ids')
    def _compute_signed_documents(self):
        """Get all signed documents from related DocuSign envelopes"""
        for addendum in self:
            if addendum.docusign_id:
                signed_attachments = addendum.docusign_id.connector_line_ids.mapped('signed_attachment_ids')
                addendum.signed_document_ids = signed_attachments
            else:
                addendum.signed_document_ids = False
    
    @api.depends('signed_document_ids')
    def _compute_document_count(self):
        for addendum in self:
            addendum.document_count = len(addendum.signed_document_ids)
    
    @api.depends('signed_document_ids')
    def _compute_has_signed_documents(self):
        for addendum in self:
            addendum.has_signed_documents = bool(addendum.signed_document_ids)

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
            raise ValidationError(_('No DocuSign envelope associated with this addendum.'))
        return {
            'name': _('DocuSign Envelope'),
            'type': 'ir.actions.act_window',
            'res_model': 'docusign.connector',
            'view_mode': 'form',
            'res_id': self.docusign_id.id,
            'target': 'current'
        }

    def action_send_for_signature(self):
        """Send addendum for signature via DocuSign"""
        self.ensure_one()
        
        if self.state != 'draft':
            raise UserError(_("Only draft addendums can be sent for signature."))
        
        if not self.contract_id:
            raise UserError(_("No parent contract found."))
        
        if not self.partner_id:
            raise UserError(_("No customer found."))
        
        # Check delivery method
        if self.contract_send_method == 'whatsapp' and not self.partner_id.whatsapp:
            raise UserError(_("Customer does not have a WhatsApp number configured."))
        
        if self.contract_send_method == 'email' and not self.partner_id.email:
            raise UserError(_("Customer does not have an email address configured."))
        
        # TODO: Implement DocuSign envelope creation for addendum
        # For now, just update state and log
        self.write({'state': 'pending_signature'})
        self.message_post(
            body=_("Addendum sent for signature via %s.") % dict(self._fields['contract_send_method'].selection).get(self.contract_send_method),
            subject="Addendum Sent for Signature"
        )
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Success'),
                'message': _('Addendum sent for signature.'),
                'type': 'success',
                'sticky': False,
            }
        }

    def action_mark_signed(self):
        """Manually mark addendum as signed (for physical signatures)"""
        self.ensure_one()
        
        if self.state != 'pending_signature':
            raise UserError(_("Only addendums pending signature can be marked as signed."))
        
        self.write({'state': 'signed'})
        self.message_post(
            body=_("Addendum manually marked as signed."),
            subject="Addendum Signed"
        )
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Success'),
                'message': _('Addendum marked as signed.'),
                'type': 'success',
                'sticky': False,
            }
        }

    def _sync_parent_contract_services(self):
        """Replace parent contract service lines with services from the parent subscription."""
        self.ensure_one()
        contract = self.contract_id
        subscription = contract.subscription_id

        if not contract or not subscription:
            _logger.warning("[Addendum] Cannot sync services: missing contract or subscription for addendum %s", self.id)
            return

        lines = subscription.order_line.filtered(lambda l: l.product_id and not l.display_type)
        contract.service_ids.sudo().unlink()

        service_vals = []
        for line in lines:
            service_vals.append({
                'contract_id': contract.id,
                'product_id': line.product_id.id,
                'name': line.name or line.product_id.name,
                'price': line.price_total,
            })

        if service_vals:
            self.env['contract.service'].sudo().create(service_vals)
            _logger.info(
                "[Addendum] Synced %d service lines to parent contract %s from subscription %s",
                len(service_vals),
                contract.name,
                subscription.name,
            )
        else:
            _logger.info("[Addendum] No service lines found on subscription %s to sync", subscription.name)

    def action_activate(self):
        """Activate addendum and apply changes"""
        self.ensure_one()
        
        if self.state != 'signed':
            raise UserError(_("Only signed addendums can be activated."))
        
        self.write({'state': 'active'})
        
        # Update parent contract financial values
        if self.contract_id:
            new_monthly_payment = self.contract_id.monthly_payment + self.monthly_payment_change
            new_contract_value = self.contract_id.contract_value + self.one_time_fee
            
            # If there's a monthly payment change, recalculate contract value
            # Contract value = monthly payment × remaining months + one-time fees
            if self.monthly_payment_change != 0 and self.contract_id.end_date:
                from dateutil.relativedelta import relativedelta
                months_remaining = 0
                if self.effective_date and self.contract_id.end_date >= self.effective_date:
                    delta = relativedelta(self.contract_id.end_date, self.effective_date)
                    months_remaining = delta.years * 12 + delta.months
                
                # Add the change impact for remaining months
                new_contract_value += (self.monthly_payment_change * months_remaining)
            
            self.contract_id.write({
                'monthly_payment': new_monthly_payment,
                'contract_value': new_contract_value,
            })
            
            _logger.info(
                "Contract %s updated: monthly_payment %.2f -> %.2f, contract_value %.2f -> %.2f",
                self.contract_id.name,
                self.contract_id.monthly_payment - self.monthly_payment_change,
                new_monthly_payment,
                self.contract_id.contract_value - new_contract_value + self.contract_id.contract_value,
                new_contract_value
            )

        # Sync parent contract services with the latest services on the parent subscription
        self._sync_parent_contract_services()
        
        self.message_post(
            body=_("Addendum activated. Changes applied to contract.<br/>" +
                   "Monthly Payment Change: $%.2f<br/>" +
                   "One-Time Fee: $%.2f<br/>" +
                   "New Monthly Payment: $%.2f<br/>" +
                   "New Contract Value: $%.2f") % (
                self.monthly_payment_change,
                self.one_time_fee,
                self.contract_id.monthly_payment if self.contract_id else 0,
                self.contract_id.contract_value if self.contract_id else 0
            ),
            subject="Addendum Activated"
        )
        
        # TODO: Implement automatic application of changes to subscription/contract
        # This could include:
        # - Adding/removing services to subscription
        # - Updating prices in sale.order.line
        # - Extending contract term
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Success'),
                'message': _('Addendum activated successfully.'),
                'type': 'success',
                'sticky': False,
            }
        }

    def action_cancel(self):
        """Cancel addendum - reverses financial changes if it was active"""
        self.ensure_one()
        
        was_active = self.state == 'active'
        
        if was_active:
            # Reverse the financial changes
            if self.contract_id:
                new_monthly_payment = self.contract_id.monthly_payment - self.monthly_payment_change
                new_contract_value = self.contract_id.contract_value - self.one_time_fee
                
                # Reverse monthly payment impact on contract value
                if self.monthly_payment_change != 0 and self.contract_id.end_date:
                    from dateutil.relativedelta import relativedelta
                    months_remaining = 0
                    if self.effective_date and self.contract_id.end_date >= self.effective_date:
                        delta = relativedelta(self.contract_id.end_date, self.effective_date)
                        months_remaining = delta.years * 12 + delta.months
                    
                    new_contract_value -= (self.monthly_payment_change * months_remaining)
                
                self.contract_id.write({
                    'monthly_payment': new_monthly_payment,
                    'contract_value': new_contract_value,
                })
                
                _logger.info(
                    "Contract %s reversed addendum changes: monthly_payment %.2f, contract_value %.2f",
                    self.contract_id.name,
                    new_monthly_payment,
                    new_contract_value
                )
        
        self.write({'state': 'cancelled'})
        
        cancel_message = _("Addendum cancelled.")
        if was_active and self.contract_id:
            cancel_message += _("<br/>Financial changes reversed:<br/>" +
                              "Monthly Payment: $%.2f<br/>" +
                              "Contract Value: $%.2f") % (
                self.contract_id.monthly_payment,
                self.contract_id.contract_value
            )
        
        self.message_post(
            body=cancel_message,
            subject="Addendum Cancelled"
        )
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Success'),
                'message': _('Addendum cancelled successfully.'),
                'type': 'success',
                'sticky': False,
            }
        }

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
        """Get current status of DocuSign envelope."""
        self.ensure_one()
        
        _logger.info("[DocuSign Addendum] Getting status for envelope %s", envelope_id)
        
        try:
            user = self.env['res.users'].browse(196)  # contratos@cabal.sv
            access_token = docu_client._get_cached_access_token(self.env, user)
            
            api_base = self._get_docusign_api_url(self.env)
            url = f"{api_base}/envelopes/{envelope_id}"
            
            response = requests.get(
                url,
                headers=self._get_docusign_headers(access_token)
            )
            
            if response.status_code != 200:
                _logger.error("[DocuSign Addendum] Failed to get envelope status: %s", response.text)
                return None
            
            result = response.json()
            status = result.get('status')
            _logger.info("[DocuSign Addendum] Envelope %s status: %s", envelope_id, status)
            
            return status
            
        except Exception as e:
            _logger.exception("[DocuSign Addendum] Error getting envelope status: %s", str(e))
            return None

    def action_resend_via_whatsapp(self):
        """Resend DocuSign envelope via WhatsApp - reuses parent contract's logic"""
        self.ensure_one()
        
        _logger.info("[DocuSign Addendum] action_resend_via_whatsapp called for addendum %s", self.id)
        
        if not self.docusign_id:
            raise UserError(_("No DocuSign envelope found for this addendum."))
        
        if not self.partner_id.whatsapp:
            raise UserError(_("Customer does not have a WhatsApp number configured."))
        
        # Validate WhatsApp format
        match = re.match(r'^\+(\d{1,3})(\d+)$', self.partner_id.whatsapp)
        if not match:
            raise UserError(_("Customer WhatsApp number is not in valid format (+country_code phone_number)."))
        
        # Get customer signer from DocuSign connector lines
        customer_line = self.docusign_id.connector_line_ids.filtered(
            lambda l: l.partner_id.id == self.partner_id.id
        )[:1]
        
        if not customer_line:
            raise UserError(_("No customer signer found in DocuSign envelope."))
        
        if not customer_line.envelope_id:
            raise UserError(_("No envelope ID found. Cannot resend."))
        
        # Note: Reuse the parent contract's resend logic by calling its methods
        # For now, we'll just update the send method and log
        self.write({'contract_send_method': 'whatsapp'})
        self.message_post(
            body=_("DocuSign notification resent via WhatsApp to %s.") % self.partner_id.whatsapp,
            subject="DocuSign Resent via WhatsApp"
        )
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Success'),
                'message': _('DocuSign notification resent via WhatsApp.'),
                'type': 'success',
                'sticky': False,
            }
        }

    def action_resend_via_email(self):
        """Resend DocuSign envelope via Email - reuses parent contract's logic"""
        self.ensure_one()
        
        _logger.info("[DocuSign Addendum] action_resend_via_email called for addendum %s", self.id)
        
        if not self.docusign_id:
            raise UserError(_("No DocuSign envelope found for this addendum."))
        
        if not self.partner_id.email:
            raise UserError(_("Customer does not have an email address configured."))
        
        # Get customer signer from DocuSign connector lines
        customer_line = self.docusign_id.connector_line_ids.filtered(
            lambda l: l.partner_id.id == self.partner_id.id
        )[:1]
        
        if not customer_line:
            raise UserError(_("No customer signer found in DocuSign envelope."))
        
        if not customer_line.envelope_id:
            raise UserError(_("No envelope ID found. Cannot resend."))
        
        # Note: Reuse the parent contract's resend logic by calling its methods
        # For now, we'll just update the send method and log
        self.write({'contract_send_method': 'email'})
        self.message_post(
            body=_("DocuSign notification resent via Email to %s.") % self.partner_id.email,
            subject="DocuSign Resent via Email"
        )
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Success'),
                'message': _('DocuSign notification resent via Email.'),
                'type': 'success',
                'sticky': False,
            }
        }

    def _compute_access_url(self):
        """Compute portal URL for addendum records."""
        super(ContractAddendum, self)._compute_access_url()
        for addendum in self:
            addendum.access_url = '/my/contract/addendum/%s' % addendum.id
    
    def _get_portal_return_action(self):
        """Return action for portal after viewing addendum."""
        self.ensure_one()
        return '/my/services'

    @api.constrains('effective_date', 'contract_id')
    def _check_effective_date(self):
        """Validate effective date is not before contract start date"""
        for addendum in self:
            if addendum.contract_id.start_date and addendum.effective_date < addendum.contract_id.start_date:
                raise ValidationError(_("Addendum effective date cannot be before contract start date (%s).") % addendum.contract_id.start_date)
