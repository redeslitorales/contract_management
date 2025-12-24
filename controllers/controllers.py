import json
import jwt
import time
import requests
from odoo import http, models, fields, _
from odoo.http import request
from odoo.exceptions import ValidationError

class DocuSignWebhookController(http.Controller):

    @http.route('/docusign/webhook', type='json', auth='public', methods=['POST'], csrf=False)
    def docusign_webhook(self, **kwargs):
        # Get the JSON data from the webhook
        data = json.loads(request.httprequest.data)
        
        # Extract the event and envelope ID
        event = data.get('event')
        envelope_id = data.get('data', {}).get('envelopeId')
        
        if event and envelope_id:
            # Find the corresponding record in docusign.connector
            docusign_connector_line = request.env['docusign.connector.line'].search([('envelope_id', '=', envelope_id)], limit=1)
            if docusign_connector_line:
                docusign_connector = request.env['docusign.connector'].browse(docusign_connector_line.record_id)

            
            if docusign_connector:
                # Authenticate
                private_key = 'MIIEogIBAAKCAQEAn453RHtmw89x082ggAlCvAvDXfrOFe630KDvbjpEJz0Cc+zGuXOsRUw4PAjxMLrUsE1NH/aN6Trm5UA7CgMtZiR/gDEjBS468nZ6soHD55O5TSCz7M75JekWEizAB5iM/ncx7qAse28ngqs7D7CtEJ+VhnHqNCIVZYMSfyCV/z9HMXDDf+sLifSPCubrziFAnVMy2iAt7MbUoHN+dF+otOPJtr4Nothf/U84PauHzTNAnlDi8u90DIAIpyq0jV2s2Cb1vlPY8zaAW4QWBZKzsN0PAuUExMzsjDxES8qNnY8n/iwD8s0pfTGUtUePTCa41w0Tfab0RVQlXV2UL07V1wIDAQABAoIBAAJtU3SEuTlbfSlFVJHHnxUm/qexNxPpVJcSI6I8tUJ9ijCDCdQFv1L/uxx0RffBfjCL9HmuF+/tl/G+/a1Q0A6tZnOBcj1tbxmDr0K3RTVPSQw0Mth0a4cymcXYzWqWkCp8wkZiRFrz6jKk8fO/co2xFI0XfxvTThnQ5rKgjcRbE88wL4L7hHdq0qeaoBYPxCLuDlZ6/GtjC10vrfrDgzcSK0z2A2bSL/aoRkwoMED8R7bQNITcDU4VlvqMWf/JSyFzKl5n8AgEWBzvUFX+/9yZ5OgWeeyIjzWBQVfYQ+hAZ3DbnT2wjNdMq3KSjgu7KN9NPdS/0/8V0aXLW4ZwFEkCgYEAyxsBv4q6ivLebLV5JcqNbz/iG0HQgzLRC7GlmH0BJm1D0IS84ayVIxD4oVZH53pYXwGfWaNGdWMcR1CuJQkaAsy29hFxJJ7BoGVY2wfFuD79wF6F0LBXKml2cil04nVOOUA0rh9USjouHUQMnSfp9X/Rs2thyrw9s0gHsxkbp68CgYEAyRwPDQFUFOgbvU2gmJB+YDi/XzS+yFeKGOQpZ02nmTdFYhAnwIkM06070TbR3bRgnuyAMpMTmZPR9LaFQPqL5Yh/h7UgiATA2mT+wshaNIfJz7ZgsJkMTpY2NUh3easWnVJhULb+6WsLmE0mWJtWhNIhiH5Qk/LH70ScPIHfllkCgYAGTOsr9vC8eLY/pw2AB52FkvS/pbYDK+NiOnuJlG8hswgEgumdEo55zP/5eUS3wIrXP6Si0jbQU2fAKpeMXJDq/1C5p2bcHPSitiIggUg34/RZMFV0WNQLY8Qh3HlcwQjRK9W2hRBHUTC3BbJiead/TxzBNRaOhHJhil16x8+czwKBgHMEChONA/JlAKBWSheW47/SFJi1iLr5XbB4pLlA7y4wLw0zYhi6CMzy0TgI2yOpqmyZo4PJG7eEk9oZnMIZyHKAizcovq8r0MPWaOErRnOuiRuzGT9GeIRlYiE9DZ9W2rskxyUrU0RZhSsYTGr2hBe4OZdfbmP+wJu1qIjuWdoZAoGAR5rR2vBBdHk+nAZ1x3Qfd7btE2tJdTEU6+xEvHiliHI9uG0IhUGfC8F/8k6jh/QnDtGeDOcJAo9lTuRR7y4UH6KLsu3Hwg4sTTj0WkbVS+vEdHnxK75tseitvstI/uNYC+Hppd+SQSm/dqCkWiiQ80w/l4DwRPiw6feGcYJXuC4='
                client_id = 'f485fbb1-e451-430c-8983-656e73083214'
                user_id = '420a4d0f-1cb1-4bef-ac17-bc9f61de1be7'
                account_id = '5007e300-b469-4789-b2e6-d9c69d03fdbd'

                # Create the JWT assertion
                now = int(time.time())
                payload = {
                    'iss': client_id,
                    'sub': user_id,
                    'aud': 'account-d.docusign.com',
                    'iat': now,
                    'exp': now + 3600,
                    'scope': 'signature impersonation'
                }
                jwt_assertion = jwt.encode(payload, private_key, algorithm='RS256')

                # Request an access token
                url = 'https://account-d.docusign.com/oauth/token'
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
                docusign_connector.status_docs()
                if event == 'envelope-completed':
                    docusign_connector.download_documents()
        
        return {'status': 'success'}