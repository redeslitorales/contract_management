import json
import jwt
import time
import requests
from odoo import http, models, fields, _
from odoo.http import request
from odoo.exceptions import ValidationError

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