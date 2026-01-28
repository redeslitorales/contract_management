import json
import jwt
import time
import requests
import logging
import hmac
import hashlib
from urllib.parse import urlparse, urljoin
from odoo.addons.odoo_docusign.models import docu_client
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

    def _build_return_url(self, contract_id):
        """Return the DocuSign embedded return URL (explicit or auto-built)."""
        IrConfigParameter = request.env['ir.config_parameter'].sudo()
        base_return = IrConfigParameter.get_param('contract_management.docusign_embedded_return_url')
        if base_return:
            return f"{base_return}?contract_id={contract_id}"

        base_host = request.httprequest.host_url.rstrip('/')
        return f"{base_host}/docusign/return?contract_id={contract_id}"

    def _start_embedded_signing(self, contract_sudo, line_sudo, source='portal'):
        """Centralized embedded signing launch (portal, magic-link, in-person)."""
        if not line_sudo.envelope_id:
            raise ValidationError(_('Falta el sobre (envelope) de DocuSign para este destinatario.'))

        client_user_id = line_sudo.client_user_id or str(contract_sudo.id)
        updates = {}
        if not line_sudo.client_user_id:
            updates['client_user_id'] = client_user_id
        if updates:
            line_sudo.write(updates)
        if not contract_sudo.docusign_client_user_id:
            contract_sudo.sudo().write({'docusign_client_user_id': client_user_id})

        return_url = self._build_return_url(contract_sudo.id)
        signer_email = line_sudo._get_recipient_email()
        signer_name = line_sudo.partner_id.name

        env_sudo = request.env['ir.config_parameter'].sudo().env
        signing_url = docu_client.create_recipient_view(
            env_sudo,
            request.env.user,
            line_sudo.envelope_id,
            signer_name,
            signer_email,
            client_user_id,
            return_url,
        )
        _logger.info(
            "[EmbeddedSign] Source=%s contract=%s envelope=%s raw_url=%s",
            source,
            contract_sudo.id,
            line_sudo.envelope_id,
            signing_url,
        )

        parsed = urlparse(signing_url)
        needs_normalization = (
            (not parsed.scheme or parsed.scheme.lower() not in ('http', 'https'))
            or not parsed.netloc
            or parsed.netloc == request.httprequest.host.split(':')[0]
        )
        _logger.info(
            "[EmbeddedSign] Parsed signing URL scheme=%s netloc=%s path=%s needs_normalization=%s",
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            needs_normalization,
        )
        if needs_normalization:
            config = docu_client._get_docusign_config(request.env)
            base_uri = (config.get('base_uri') or '').rstrip('/')
            if base_uri:
                base_parts = urlparse(base_uri)
                base_root = f"{base_parts.scheme}://{base_parts.netloc}" if base_parts.scheme and base_parts.netloc else base_uri
                signing_url = urljoin(base_root + '/', signing_url.lstrip('/'))
                _logger.info("[EmbeddedSign] Normalized signing URL to %s", signing_url)
            else:
                _logger.warning("[EmbeddedSign] Missing base_uri while normalizing signing URL %s", signing_url)

        now = fields.Datetime.now()
        line_sudo.write({
            'embedded_signing_url': signing_url,
            'embedded_started_at': now,
            'embedded_event': f'started:{source}',
        })
        contract_sudo.sudo().write({
            'docusign_embedded_signing_url': signing_url,
            'docusign_embedded_status': 'started',
        })

        contract_sudo.message_post(
            body=(
                _("Embedded signing link generated (%s). If needed, copy this URL for the customer: %s")
                % (source, signing_url)
            )
        )

        return signing_url

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
        
        has_embedded = bool(contract_sudo.docusign_client_user_id)
        is_completed = contract_sudo.docusign_status == 'completed' and contract_sudo.has_signed_documents
        if not (has_embedded or is_completed):
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

    @http.route(['/my/contracts/<int:contract_id>/sign'], type='http', auth='user', website=True, sitemap=False)
    def portal_contract_sign(self, contract_id=None, access_token=None, **kw):
        """Start embedded signing for a contract using DocuSign Recipient View."""
        try:
            contract_sudo = self._document_check_access('contract.management', contract_id, access_token)
        except (AccessError, ValidationError):
            _logger.warning("[PortalSign] Access check failed for contract %s", contract_id)
            return request.redirect('/my')

        if contract_sudo.partner_id != request.env.user.partner_id:
            _logger.warning(
                "[PortalSign] Partner mismatch. Contract partner %s, user partner %s",
                contract_sudo.partner_id.id,
                request.env.user.partner_id.id,
            )
            return request.redirect('/my')

        connector = contract_sudo.docusign_id.sudo()
        if not connector:
            _logger.error("[PortalSign] Missing connector for contract %s", contract_sudo.id)
            return request.render('contract_management.portal_sign_error', {
                'contract': contract_sudo,
                'reason': _('No se encontró el registro de DocuSign para este contrato.'),
            })

        line = connector.connector_line_ids.filtered(lambda l: l.partner_id == contract_sudo.partner_id)[:1]
        if not line:
            _logger.error("[PortalSign] Missing connector line for partner %s on connector %s", contract_sudo.partner_id.id, connector.id)
            return request.render('contract_management.portal_sign_error', {
                'contract': contract_sudo,
                'reason': _('No se encontró un destinatario de DocuSign asociado a este cliente.'),
            })

        if not line.envelope_id:
            _logger.error("[PortalSign] Missing envelope_id on line %s for contract %s", line.id, contract_sudo.id)
            return request.render('contract_management.portal_sign_error', {
                'contract': contract_sudo,
                'reason': _('El sobre (envelope) de DocuSign aún no existe para este contrato. Reenvíe el contrato desde Odoo para generar el sobre.'),
            })

        if not line.client_user_id:
            _logger.error("[PortalSign] Missing client_user_id on line %s for contract %s", line.id, contract_sudo.id)
            return request.render('contract_management.portal_sign_error', {
                'contract': contract_sudo,
                'reason': _('Falta el identificador de firma embebida (client_user_id). Reenvíe el contrato desde Odoo para regenerarlo.'),
            })

        try:
            signing_url = self._start_embedded_signing(contract_sudo, line.sudo(), source='portal')
        except ValidationError:
            _logger.exception(
                "[PortalSign] DocuSign recipient view failed for contract %s, envelope %s, client_user_id %s",
                contract_sudo.id,
                line.envelope_id,
                line.client_user_id,
            )
            return request.redirect('/my/contract/%s' % contract_id)
        return request.redirect(signing_url, local=False)

    @http.route(['/contracts/sign/<string:token>'], type='http', auth='public', website=True, csrf=False)
    def contract_sign_magic_link(self, token=None, **kw):
        """Magic-link signing entry point (no login required)."""
        line, error = request.env['docusign.connector.lines'].resolve_magic_token(token)
        if error:
            reason_map = {
                'missing': _('Falta el token de firma.'),
                'not_found': _('El enlace de firma ya no es válido.'),
                'used': _('Este enlace ya fue usado.'),
                'expired': _('Este enlace de firma ha expirado. Solicite uno nuevo.'),
            }
            return request.render('contract_management.portal_sign_error', {
                'contract': False,
                'reason': reason_map.get(error, _('No se pudo validar el enlace de firma.')),
            })

        line = line.sudo()
        connector = line.record_id
        contract = request.env['contract.management'].sudo().search([('docusign_id', '=', connector.id)], limit=1)
        if not contract:
            return request.render('contract_management.portal_sign_error', {
                'contract': False,
                'reason': _('No se encontró un contrato asociado a este enlace.'),
            })
        if line.partner_id != contract.partner_id:
            return request.render('contract_management.portal_sign_error', {
                'contract': contract,
                'reason': _('El enlace no corresponde a este cliente.'),
            })

        try:
            signing_url = self._start_embedded_signing(contract, line, source='magic-link')
            line.consume_magic_token()
        except ValidationError as exc:
            _logger.exception("[MagicLinkSign] Failed to start signing for contract %s: %s", contract.id, exc)
            return request.render('contract_management.portal_sign_error', {
                'contract': contract,
                'reason': _('No se pudo iniciar la firma. Reenvíe el enlace.'),
            })

        return request.redirect(signing_url, local=False)

    @http.route(['/contracts/sign/in_person/<int:contract_id>'], type='http', auth='user', website=True, sitemap=False)
    def contract_sign_in_person(self, contract_id=None, **kw):
        """In-person embedded signing launch for staff on a shared device."""
        if not request.env.user.has_group('base.group_user'):
            return request.redirect('/my')

        contract = request.env['contract.management'].sudo().browse(int(contract_id))
        if not contract or not contract.exists():
            return request.redirect('/my')

        connector = contract.docusign_id
        line = connector.connector_line_ids.filtered(lambda l: l.partner_id == contract.partner_id)[:1]
        if not line:
            return request.render('contract_management.portal_sign_error', {
                'contract': contract,
                'reason': _('No se encontró un destinatario de DocuSign asociado a este cliente.'),
            })

        try:
            signing_url = self._start_embedded_signing(contract, line.sudo(), source='in-person')
        except ValidationError as exc:
            _logger.exception("[InPersonSign] Failed to start signing for contract %s: %s", contract.id, exc)
            return request.render('contract_management.portal_sign_error', {
                'contract': contract,
                'reason': _('No se pudo iniciar la firma en persona.'),
            })

        return request.redirect(signing_url, local=False)

    @http.route(['/docusign/return'], type='http', auth='public', website=True, csrf=False)
    def docusign_return(self, contract_id=None, event=None, **kw):
        """Handle DocuSign return URL for embedded signing."""
        if not contract_id:
            return request.redirect('/my')

        contract = request.env['contract.management'].sudo().browse(int(contract_id))
        if not contract or not contract.exists():
            return request.redirect('/my')

        connector = contract.docusign_id
        line = connector.connector_line_ids.filtered(lambda l: l.partner_id == contract.partner_id)[:1]

        event = event or request.params.get('event') or request.params.get('eventParam')
        new_status = 'started'
        completed_at = False
        if event in ('signing_complete', 'completed'):
            new_status = 'completed'
            completed_at = fields.Datetime.now()
        elif event in ('cancel', 'canceled'):
            new_status = 'canceled'
        elif event in ('decline', 'declined'):
            new_status = 'declined'

        if line:
            line.write({
                'embedded_event': event,
                'embedded_completed_at': completed_at,
            })

        contract.write({
            'docusign_embedded_status': new_status,
        })

        return request.render('contract_management.docusign_return_page', {
            'contract': contract,
            'event': event,
            'status': new_status,
        })


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
