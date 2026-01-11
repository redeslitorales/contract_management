import json
import jwt
import time
import requests
from odoo import http, models, fields, _
from odoo.http import request
from odoo.exceptions import ValidationError, AccessError
from odoo.addons.portal.controllers.portal import CustomerPortal, pager as portal_pager

# DEPRECATED: This webhook controller has been disabled in favor of the odoo_docusign webhook
# which includes HMAC signature verification for improved security.
# The odoo_docusign webhook at /docusign/webhook should be used instead.
# See odoo_docusign/controllers/webhook_controller.py for the active implementation.
#
# To re-enable this webhook (not recommended), uncomment the class below and change the route
# to avoid conflicts (e.g., '/contract_management/docusign/webhook')

# class DocuSignWebhookController(http.Controller):
#
#     @http.route('/docusign/webhook/legacy', type='json', auth='public', methods=['POST'], csrf=False)
#     def docusign_webhook(self, **kwargs):
#         """
#         LEGACY WEBHOOK - Replaced by odoo_docusign webhook with HMAC security.
#         This code is preserved for reference but should not be used in production.
#         """
#         # Get the JSON data from the webhook
#         data = json.loads(request.httprequest.data)
#         
#         # Extract the event and envelope ID
#         event = data.get('event')
#         envelope_id = data.get('data', {}).get('envelopeId')
#         
#         if event and envelope_id:
#             # Find the corresponding record in docusign.connector
#             # NOTE: Fixed model name from 'docusign.connector.line' to 'docusign.connector.lines'
#             docusign_connector_line = request.env['docusign.connector.lines'].search([('envelope_id', '=', envelope_id)], limit=1)
#             if docusign_connector_line:
#                 docusign_connector = request.env['docusign.connector'].browse(docusign_connector_line.record_id)
#
#             
#             if docusign_connector:
#                 # Get credentials from settings instead of hardcoding
#                 ICP = request.env['ir.config_parameter'].sudo()
#                 private_key = ICP.get_param('docusign_private_key', default='')
#                 client_id = ICP.get_param('docusign_integration_key', default='')
#                 user_id = ICP.get_param('docusign_user_id', default='')
#                 account_id = ICP.get_param('docusign_account_id', default='')
#                 
#                 if not all([private_key, client_id, user_id, account_id]):
#                     raise ValidationError(_("DocuSign credentials not configured in Settings"))
#
#                 # Create the JWT assertion
#                 now = int(time.time())
#                 payload = {
#                     'iss': client_id,
#                     'sub': user_id,
#                     'aud': 'account-d.docusign.com',
#                     'iat': now,
#                     'exp': now + 3600,
#                     'scope': 'signature impersonation'
#                 }
#                 jwt_assertion = jwt.encode(payload, private_key, algorithm='RS256')
#
#                 # Request an access token
#                 url = 'https://account-d.docusign.com/oauth/token'
#                 headers = {
#                     'Content-Type': 'application/x-www-form-urlencoded'
#                 }
#                 data = {
#                     'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
#                     'assertion': jwt_assertion
#                 }
#                 response = requests.post(url, headers=headers, data=data)
#                 access_token = response.json().get('access_token')
#
#                 if not access_token:
#                     raise ValidationError(_("Failed to obtain access token from DocuSign"))
#
#                 # Trigger the method from docu_client.py using JWT authentication
#                 docusign_connector.status_docs()
#                 if event == 'envelope-completed':
#                     docusign_connector.download_documents()
#         
#         return {'status': 'success'}


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
