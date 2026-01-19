import json
import jwt
import time
import requests
import logging
import hmac
import hashlib
from odoo import http, models, fields, _
from odoo.http import request
from odoo.exceptions import ValidationError, AccessError
from odoo.addons.portal.controllers.portal import CustomerPortal, pager as portal_pager

_logger = logging.getLogger(__name__)

# DEPRECATED: This webhook controller has been disabled in favor of the odoo_docusign webhook
# which includes HMAC signature verification for improved security.
# The odoo_docusign webhook at /docusign/webhook should be used instead.
# See odoo_docusign/controllers/webhook_controller.py for the active implementation.
#
# To re-enable this webhook (not recommended), uncomment the class below and change the route
# to avoid conflicts (e.g., '/contract_management/docusign/webhook')

class DocuSignWebhookController(http.Controller):

    @http.route('/docusign/webhook1', type='json', auth='public', methods=['POST'], csrf=False)
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
                        if subscription.contract_state in ['pending_customer_signature', 'pending_contract', 'pending_cabal_signature']:
                            subscription.contract_state = 'pending_cabal_signature'
                        else:
                            # Post warning to subscription chatter
                            subscription.message_post(
                                body=_("Could not update contract state. Current state is '%s' but should have been 'pending_customer_signature' for recipient-completed event.") % subscription.contract_state,
                                subject=_('DocuSign Contract State Mismatch'),
                                message_type='notification',
                                subtype_xmlid='mail.mt_note'
                            )
                            _logger.warning(
                                "[DocuSign Webhook] Contract state mismatch for subscription %s: current=%s, expected=pending_customer_signature",
                                subscription.id,
                                subscription.contract_state,
                            )
                    if event == 'envelope-completed':
                        connector.state = 'completed'
                        subscription = request.env['sale.order'].sudo().browse(connector.sale_id.id)
                        if subscription.contract_state in ['pending_cabal_signature', 'pending_customer_signature', 'pending_contract']:
                            # Auto-create installation task and move to schedule state
                            try:
                                subscription.action_create_install_task()
                                _logger.info("[DocuSign Webhook] Installation task auto-created for subscription %s", subscription.id)
                            except Exception as e:
                                _logger.warning("[DocuSign Webhook] Failed to auto-create install task: %s", str(e))
                                # If task creation fails, still advance state manually
                                subscription.write({'installation_state': 'to_be_scheduled'})

                            # Mark contract active after full completion
                            subscription.contract_state = 'active'
                        else:
                            # Post warning to subscription chatter
                            subscription.message_post(
                                body=_("Could not update contract state. Current state is '%s' but should have been 'pending_cabal_signature' for envelope-completed event.") % subscription.contract_state,
                                subject=_('DocuSign Contract State Mismatch'),
                                message_type='notification',
                                subtype_xmlid='mail.mt_note'
                            )
                            _logger.warning(
                                "[DocuSign Webhook] Contract state mismatch for subscription %s: current=%s, expected=pending_cabal_signature",
                                subscription.id,
                                subscription.contract_state,
                            )
                        
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
    
class ContractPortal(CustomerPortal):
    """Portal controller for contract management customer portal views."""

    def _prepare_home_portal_values(self, counters):
        """Add contract counts to portal home."""
        values = super()._prepare_home_portal_values(counters)
        partner = request.env.user.partner_id
        
        ContractManagement = request.env['contract.management']
        
        if 'contract_count' in counters:
            # Count contracts with completed signatures and signed documents
            values['contract_count'] = ContractManagement.search_count([
                ('partner_id', '=', partner.id),
                ('docusign_status', '=', 'completed'),
                ('has_signed_documents', '=', True)
            ])
        
        return values

    @http.route(['/my/contract/<int:contract_id>'], type='http', auth='user', website=True, sitemap=False)
    def portal_my_contract(self, contract_id=None, access_token=None, **kw):
        """Display contract details in customer portal."""
        try:
            contract_sudo = self._document_check_access('contract.management', contract_id, access_token)
        except (AccessError, ValidationError):
            return request.redirect('/my')
        
        # Verify contract belongs to current user's partner
        if contract_sudo.partner_id != request.env.user.partner_id:
            return request.redirect('/my')
        
        # Verify signature is completed and documents exist
        if contract_sudo.docusign_status != 'completed' or not contract_sudo.has_signed_documents:
            return request.redirect('/my/services')
        
        values = {
            'contract': contract_sudo,
            'page_name': 'contract',
        }
        
        return request.render('contract_management.portal_my_contract', values)
    
    @http.route(['/my/contract/<int:contract_id>/download/<int:attachment_id>'], type='http', auth='user', website=True)
    def portal_contract_download_document(self, contract_id=None, attachment_id=None, access_token=None, **kw):
        """Download signed contract document from portal."""
        try:
            contract_sudo = self._document_check_access('contract.management', contract_id, access_token)
        except (AccessError, ValidationError):
            return request.redirect('/my')
        
        # Verify contract belongs to current user's partner
        if contract_sudo.partner_id != request.env.user.partner_id:
            return request.redirect('/my')
        
        # Verify attachment belongs to this contract
        attachment = request.env['ir.attachment'].sudo().browse(attachment_id)
        if attachment not in contract_sudo.signed_document_ids:
            return request.redirect('/my/contract/%s' % contract_id)
        
        # Stream the file directly
        if not attachment.exists():
            return request.redirect('/my/contract/%s' % contract_id)
            
        return http.Stream.from_attachment(attachment).get_response()


class SaleOrderConfirmationController(http.Controller):
    """Handle public sale order confirmations via webhook links."""

    @http.route('/webhook/confirm_sale_order', type='http', auth='public', methods=['GET'], csrf=False)
    def confirm_sale_order(self, uuid=None, send_method=None, **kwargs):
        """
        Public webhook endpoint for confirming sale orders via unique UUID link.
        
        Expected parameters:
            uuid: Unique confirmation UUID for the sale order
            send_method: Delivery method for contract (whatsapp, email, physical)
        
        Returns:
            Redirect to success or error page with appropriate message
        """
        try:
            # Default to WhatsApp if no method specified
            if not send_method:
                send_method = 'whatsapp'
            else:
                send_method = send_method.lower()
            
            # Validate UUID parameter
            if not uuid:
                _logger.warning("[QuoteConfirm] Confirmation attempted without UUID. IP: %s", 
                              request.httprequest.remote_addr)
                return request.redirect('/quote_reject?reason=missing_uuid')

            exp_param = kwargs.get('exp')
            sig_param = kwargs.get('sig') or ''
            if not exp_param:
                _logger.warning("[QuoteConfirm] Missing expiration parameter for UUID %s. IP: %s", uuid, request.httprequest.remote_addr)
                return request.redirect('/quote_reject?reason=missing_sig')
            try:
                exp_date = fields.Date.from_string(exp_param)
            except Exception:
                exp_date = False
            if not exp_date:
                _logger.warning("[QuoteConfirm] Invalid expiration format for UUID %s. IP: %s", uuid, request.httprequest.remote_addr)
                return request.redirect('/quote_reject?reason=invalid_sig')
            
            # Search for sale order with matching UUID
            sale_order = request.env['sale.order'].sudo().search([
                ('confirmation_uuid', '=', uuid)
            ], limit=1)
            
            if not sale_order:
                _logger.warning("[QuoteConfirm] No order found for UUID: %s. IP: %s", 
                              uuid, request.httprequest.remote_addr)
                return request.redirect('/quote_reject?reason=invalid_uuid')

            # Validate expiration against order validity_date
            # Use the sale order record to provide a context-aware date (handles user tz)
            today = fields.Date.context_today(sale_order)
            if sale_order.validity_date and exp_date != sale_order.validity_date:
                _logger.warning("[QuoteConfirm] Expiration mismatch for order %s. Expected %s got %s. IP: %s",
                                sale_order.name, sale_order.validity_date, exp_date, request.httprequest.remote_addr)
                return request.redirect('/quote_reject?reason=invalid_sig')
            if exp_date and exp_date < today:
                _logger.info("[QuoteConfirm] Link expired for order %s (exp=%s, today=%s). IP: %s",
                             sale_order.name, exp_date, today, request.httprequest.remote_addr)
                return request.redirect('/quote_reject?reason=expired')

            expected_sig = sale_order._sign_confirmation_payload(uuid, exp_param)
            if not expected_sig:
                _logger.error("[QuoteConfirm] HMAC secret not configured; refusing confirmation for order %s", sale_order.name)
                return request.redirect('/quote_reject?reason=invalid_sig')
            if not hmac.compare_digest(expected_sig, sig_param):
                _logger.warning("[QuoteConfirm] Invalid signature for order %s. IP: %s", sale_order.name, request.httprequest.remote_addr)
                return request.redirect('/quote_reject?reason=invalid_sig')
            
            # Validate order state
            if sale_order.state not in ['draft', 'sent']:
                _logger.warning("[QuoteConfirm] Order %s (ID: %s) in invalid state for confirmation: %s. IP: %s",
                              sale_order.name, sale_order.id, sale_order.state, request.httprequest.remote_addr)
                return request.redirect('/quote_reject?reason=invalid_state&order=%s' % sale_order.name)
            
            # Validate send method
            valid_methods = ['whatsapp', 'email', 'physical', 'donotsend']
            if send_method not in valid_methods:
                _logger.warning("[QuoteConfirm] Invalid send method '%s' for order %s. Defaulting to whatsapp.",
                              send_method, sale_order.name)
                send_method = 'whatsapp'
            
            # Auto-determine send method based on customer contact info
            # Priority: WhatsApp > Email > Physical
            if send_method == 'whatsapp' and not sale_order.partner_id.whatsapp:
                _logger.info("[QuoteConfirm] Customer %s has no WhatsApp. Falling back to email.", 
                           sale_order.partner_id.name)
                send_method = 'email' if sale_order.partner_id.email else 'physical'
            
            # Update sale order with send method and mark as confirmed
            tag = request.env.ref('contract_management.contract_quote_confirmed_tag', raise_if_not_found=False)
            tag_cmds = [(4, tag.id)] if tag else []
            sale_order.write({
                'quote_confirmed': True,
                'contract_send_method': send_method,
                'tag_ids': tag_cmds
            })
            
            _logger.info("[QuoteConfirm] Order %s (ID: %s) confirmed via webhook. Customer: %s, Send method: %s, IP: %s",
                        sale_order.name, sale_order.id, sale_order.partner_id.name, 
                        send_method, request.httprequest.remote_addr)
            
            # Automatically confirm the order and send contract
            try:
                # Call action_confirm_via_uuid which handles contract sending
                sale_order.action_confirm_via_uuid()
                _logger.info("[QuoteConfirm] ✓ Order %s auto-confirmed and contract sent via %s",
                           sale_order.name, send_method)
            except Exception as confirm_error:
                _logger.error("[QuoteConfirm] ✗ Failed to auto-confirm order %s: %s",
                            sale_order.name, str(confirm_error))
                # Still redirect to success page - order was marked confirmed
                # Manual intervention may be needed for contract sending
            
            return request.redirect('/quote_confirmed?order=%s&method=%s' % (sale_order.name, send_method))
            
        except Exception as e:
            _logger.exception("[QuoteConfirm] ✗ Unexpected error confirming order. UUID: %s, IP: %s. Error: %s",
                            uuid, request.httprequest.remote_addr, str(e))
            return request.redirect('/quote_reject?reason=system_error')

    @http.route('/quote_confirmed', type='http', auth='public', methods=['GET'], csrf=False, website=True)
    def quote_confirmed_page(self, order=None, method=None, **kwargs):
        """
        Success page shown after quote confirmation.
        
        Parameters:
            order: Order name (for display)
            method: Send method chosen (for display)
        """
        return request.render('contract_management.quote_confirmed_template', {
            'order_name': order or _('Your Order'),
            'send_method': method or 'whatsapp',
            'send_method_label': dict([
                ('whatsapp', 'WhatsApp'),
                ('email', 'Email'),
                ('physical', 'Physical Copy'),
                ('donotsend', 'Will Not Send')
            ]).get(method, 'WhatsApp')
        })

    @http.route('/quote_reject', type='http', auth='public', methods=['GET'], csrf=False, website=True)
    def quote_reject_page(self, reason=None, order=None, **kwargs):
        """
        Error page shown when quote confirmation fails.
        
        Parameters:
            reason: Error reason code
            order: Order name (if available)
        """
        error_messages = {
            'missing_uuid': _('The confirmation link is incomplete. Please use the full link sent to you.'),
            'invalid_uuid': _('This confirmation link is invalid or has expired.'),
            'invalid_state': _('This quotation has already been processed or is no longer available.'),
            'missing_sig': _('The confirmation link is missing security parameters.'),
            'invalid_sig': _('This confirmation link is not valid. Please request a new link.'),
            'expired': _('This confirmation link has expired. Please request a new link.'),
            'system_error': _('A system error occurred. Please contact support.'),
        }
        
        return request.render('contract_management.quote_rejected_template', {
            'reason_code': reason or 'unknown',
            'order_name': order,
            'error_message': error_messages.get(reason, _('Unable to confirm quotation. Please contact support.')),
            'support_phone': '+503 2563 4888',
            'support_email': 'soporte@cabalinternet.com'
        })
