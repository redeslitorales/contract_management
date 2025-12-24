from odoo.exceptions import ValidationError
from odoo import _
try:
    import httplib2
except ImportError:
    raise ValidationError(_('Package httplib2 not found.If you plan to use it, '
                            'please install the httplib2 library from https://pypi.org/project/httplib2/'))
import sys, json
import os
import requests

#root_path = os.path.dirname(os.path.abspath(__file__))
root_path = '/mnt/docusign'
headers = {'Accept': 'application/json',
           'Content-Type': 'application/json'
           }
baseUrl = 'https://demo.docusign.net/restapi/v2.1/accounts/'


def send_docusign_file(user, file_name, file_contents, receiver1_name, receiver1_email, receiver2_name, receiver2_email, send_method, country_code, phone_number):
    try:
        if send_method == 'whatsapp':
            envelope_data = {
                "emailSubject": "ACCION REQUERIDA: Firmar su contrato con Cabal Internet ahora",
                "documents": [
                    {
                        "documentBase64": file_contents.decode("utf-8"),
                        "name": file_name,
                        "fileExtension": "pdf",
                        "documentId": "1"
                    }
                ],
                "recipients": {
                    "signers": [
                        {
                            "email": receiver1_email,
                            "name": receiver1_name,
                            "recipientId": "1",
                            "routingOrder": "1",
                            "deliveryMethod": "WhatsApp",
                            "phoneNumber": {
                                "countryCode": str(country_code),
                                "number": str(phone_number)
                            },                           
                            "tabs": {
                                "signHereTabs": [
                                    {
                                        "anchorString": "/sn1/",
                                        "anchorYOffset": "0",
                                        "anchorUnits": "pixels",
                                        "documentId": "1",
                                        "pageNumber": "1"
                                    }
                                ]
                            }
                        },
                        {
                            "email": receiver2_email,
                            "name": receiver2_name,
                            "recipientId": "2",
                            "routingOrder": "2",
                            "tabs": {
                                "signHereTabs": [
                                    {
                                        "anchorString": "/sn2/",
                                        "anchorYOffset": "0",
                                        "anchorUnits": "pixels",
                                        "documentId": "1",
                                        "pageNumber": "1"
                                    }
                                ]
                            }
                        }
                    ]
                },
                "status": "sent"
            }
        else:
            envelope_data = {
                "emailSubject": "IMPORTANTE: Confirmar su servicio de internet....Firmar su contrato ahora",
                "documents": [
                    {
                        "documentBase64": file_contents.decode("utf-8"),
                        "name": file_name,
                        "fileExtension": "pdf",
                        "documentId": "1"
                    }
                ],
                "recipients": {
                    "signers": [
                        {
                            "email": receiver1_email,
                            "name": receiver1_name,
                            "recipientId": "1",
                            "routingOrder": "1",
                            "tabs": {
                                "signHereTabs": [
                                    {
                                        "anchorString": "/sn1/",
                                        "anchorYOffset": "0",
                                        "anchorUnits": "pixels",
                                        "documentId": "1",
                                        "pageNumber": "1"
                                    }
                                ]
                            }
                        },
                        {
                            "email": receiver2_email,
                            "name": receiver2_name,
                            "recipientId": "2",
                            "routingOrder": "2",
                            "tabs": {
                                "signHereTabs": [
                                    {
                                        "anchorString": "/sn2/",
                                        "anchorYOffset": "0",
                                        "anchorUnits": "pixels",
                                        "documentId": "1",
                                        "pageNumber": "1"
                                    }
                                ]
                            }
                        }
                    ]
                },
                "status": "sent"
            }
        uri = user.base_uri if user.base_uri else False
        account_id = user.account_id if user.account_id else False
#       authenticated = user.authenicate_jwt(self)
#       if not account_id or not user.access_token:
#        if not authenticated:
#            raise ValidationError(_("You need to authenticate credentials for logged-in user!"))
        if uri and account_id:
            baseUrl = uri + '/restapi/v2.1/accounts/' + account_id
            url = baseUrl + '/envelopes'
            headers = {
                'Authorization': 'Bearer ' + user.access_token,
                'Content-Type': 'application/json'
            }
            response = requests.request('POST', url, headers=headers, data=json.dumps(envelope_data))
            if response.status_code != 201:
                raise ValidationError((str(response.text)))
            data = response.json()
            envelope_id = data.get('envelopeId')
            return envelope_id
    except Exception as e:
        raise ValidationError(_(str(e)))

def get_status(user, envelopeId):
    try:
        uri = user.base_uri if user.base_uri else False
        account_id = user.account_id if user.account_id else False
        if uri and account_id:
            url = uri + '/restapi/v2.1/accounts/' + account_id + '/envelopes/' + envelopeId + "/recipients"
            headers['Authorization'] = 'Bearer ' + user.access_token
            http = httplib2.Http()
            response, content = http.request(url, 'GET', headers=headers)
            status = response.get('status')
            if status != '200':
                raise ValidationError(("Error calling webservice, status is: %s" % status))
            data = json.loads(content)
            signers = data.get('signers')
            signers = signers[0] if len(signers) >= 1 else False
            return signers['status']
    except Exception as e:
        raise ValidationError(_(str(e)))


def download_documents(user, envelopeId):
    try:
        doc_status = get_status(user, envelopeId)
        complete_path = ''
        uriList = []
        if doc_status != 'completed':
            return doc_status, complete_path

        uri = user.base_uri if user.base_uri else False
        account_id = user.account_id if user.account_id else False
        if uri and account_id:
            baseUrl = uri + '/restapi/v2/accounts/' + account_id
            envelopeUri = "/envelopes/" + envelopeId
            url = baseUrl + envelopeUri + '/documents'
            headers['Authorization'] = 'Bearer ' + user.access_token
            http = httplib2.Http()
            response, content = http.request(url, 'GET', headers=headers)
            status = response.get('status')
            if status != '200':
                raise ValidationError(("Error calling webservice, status is: %s" % status))
            data = json.loads(content)
            envelope = data.get('envelopeDocuments')
            envelope = envelope[0] if len(envelope) > 1 else False

            uriList.append(envelope.get("uri"))
            # download each document
            url = baseUrl + uriList[len(uriList) - 1]
            headers['Authorization'] = 'Bearer ' + user.access_token
            http = httplib2.Http()
            response, content = http.request(url, 'GET', headers=headers)
            status = response.get('status')

            if status != '200':
                raise ValidationError(("Error calling webservice, status is: %s" % status))

            directory_path = os.path.join(root_path, "files")
            if not os.path.isdir(directory_path):
                try:
                    os.mkdir(directory_path)
                except:
                    raise ValidationError("Unable to download attachments.\nPlease provide access rights to module.")

            attach_file_name = envelope.get("name")
            file_path = os.path.join("files", attach_file_name)
            complete_path = os.path.join(root_path, file_path)
            with open(complete_path, "wb") as text_file:
                text_file.write(content)
                text_file.close()
            # removed logic: complete_path, need 'content' only to override local storage logic
            if status == '200':
                # return doc_status, complete_path
                return doc_status, content
            else:
                raise ValidationError('Connection Failed! Please check Docusign credentials.')
    except Exception as e:
        raise ValidationError(_(str(e)))