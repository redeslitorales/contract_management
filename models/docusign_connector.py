from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
import time
import base64
import requests
import json
import re
from docusign_esign import ApiClient, EnvelopesApi, OAuth
from odoo.addons.contract_management.models import docu_client

platform_type = {
    'dev': 'account-d.docusign.com',
    'prod': 'account.docusign.com'
}

class OverrideDocumentStatus(models.Model):
    _inherit = 'docusign.connector'

    state = fields.Selection([('new', 'New'), ('open', 'Open'),('sent', 'Sent'), ('customer', 'Customer Signed'), ('completed', 'Completed')], default='new')

    def send_docs(self, send_method):
        try:
            country_code = ''
            phone_number = ''
            user = self.env['res.users'].browse(196)
#           user = self.env.user
            if not self.attachment_ids:
                raise ValidationError(_('Attachment(s) not found.'))
            if not self.connector_line_ids:
                raise ValidationError(_("No recipient(s) found for this record."))

            company_email = self.env['res.users'].sudo().browse(196).email
            company_name = self.env['res.users'].sudo().browse(196).name
            if self.docs_policy == 'in':
                next_recipient = self.connector_line_ids.filtered(lambda r: not r.send_status)

                if not next_recipient:
                    raise ValidationError(_("Document sent to all recipients."))

                line = next_recipient[0]

                if send_method == 'whatsapp':
                    if not line.partner_id.whatsapp:
                       raise ValidationError(_('WhatsApp number has not been defined for recipient: ' + str(line.partner_id.name)))
                    else:
                        if line.partner_id.whatsapp.startswith('+1') and len(line.partner_id.whatsapp) == 12:
                            match = re.match(r'^\+(\d{1})(\d{10})$', line.partner_id.whatsapp)
                        elif line.partner_id.whatsapp.startswith('+503') and len(line.partner_id.whatsapp) == 12:
                            match = re.match(r'^\+(\d{1,3})(\d+)$', line.partner_id.whatsapp)
                        else:
                            match = re.match(r'^\+(\d{1,3})(\d{4,14})$', line.partner_id.whatsapp)
                        if match:
                            country_code = match.group(1)
                            phone_number = match.group(2)
                        else:
                           raise ValidationError(_('WhatsApp number for recipient is not valid: ' + str(line.partner_id.name)))

                if not line.partner_id.email:
                    raise ValidationError(_('Email has not been defined for recipient: ' + str(line.partner_id.name)))


                previous_recipients = self.connector_line_ids.filtered(lambda r: r.id < line.id)
                incomplete_previous = previous_recipients.filtered(lambda r: not r.sign_status)

                if incomplete_previous:
                    raise ValidationError(
                        _("Please ensure the previous recipient has signed the document: %s") % incomplete_previous[
                            0].partner_id.name)


                signed_attachment = previous_recipients.filtered(lambda r: r.sign_status).sorted(key=lambda r: r.id,
                                                                                                 reverse=True).mapped(
                    'signed_attachment_ids')
                if signed_attachment:

                    attach_file = signed_attachment[0]
                else:

                    attach_file = self.attachment_ids[0]

                attach_file_name = attach_file.name
                attach_file_data = attach_file.sudo().read(['datas'])
                file_data_encoded_string = attach_file_data[0]['datas']


                envelope_id = docu_client.send_docusign_file(
                    user, attach_file_name, file_data_encoded_string, line.partner_id.name, line.partner_id.email, company_name, company_email, send_method, country_code, phone_number
                )


                line.un_signed_attachment_ids |= attach_file
                line.sudo().write({
                    'status': 'sent',
                    'name': attach_file.name,
                    'envelope_id': envelope_id,
                    'send_status': True,
                })

                self.write({'state': 'sent'})
                self.env.cr.commit()
                return self.action_of_button("Document(s) has been sent to: %s" % line.partner_id.name)

            if self.docs_policy == 'out':
                for line in self.connector_line_ids:
                    
                    if send_method == 'whatsapp':
                        if not line.partner_id.whatsapp:
                            raise ValidationError(_('WhatsApp number has not been defined for recipient: ' + str(line.partner_id.name)))
                        else:
                            if line.partner_id.whatsapp.startswith('+1') and len(line.partner_id.whatsapp) == 12:
                                match = re.match(r'^\+(\d{1})(\d{10})$', line.partner_id.whatsapp)
                            elif line.partner_id.whatsapp.startswith('+503') and len(line.partner_id.whatsapp) == 12:
                                match = re.match(r'^\+(\d{1,3})(\d+)$', line.partner_id.whatsapp)
                            else:
                                match = re.match(r'^\+(\d{1,3})(\d{4,14})$', line.partner_id.whatsapp)
                            if match:
                                country_code = match.group(1)
                                phone_number = match.group(2)
                            else:
                                raise ValidationError(_('WhatsApp number for recipient is not valid: ' + str(line.partner_id.name)))

                    if not line.partner_id.email:
                        raise ValidationError(_('Email has not been defined for recipient: ' + str(line.partner_id.name)))

                    for file in self.attachment_ids:
                        attach_file_name = file.name
                        filename, file_extension = os.path.splitext(attach_file_name)
                        if file_extension != '.pdf':
                            raise ValidationError('File extension must be .pdf')
                        attach_file_data = file.sudo().read(['datas'])
                        file_data_encoded_string = attach_file_data[0]['datas']

                        envelop_id = docu_client.send_docusign_file(
                            user, attach_file_name, file_data_encoded_string, line.partner_id.name, line.partner_id.email, company_name, company_email, send_method, country_code, phone_number)

                        line.un_signed_attachment_ids |= file
                        line.sudo().write({
                            'status': 'sent',
                            'name': file.name,
                            'envelope_id': envelop_id,
                            'send_status': True
                        })
                self.write({'state': 'sent'})
                self.env.cr.commit()
                return self.action_of_button("Document(s) has been sent successfully !")

        except Exception as e:
            raise ValidationError(_(str(e)))

    def download_docs(self):
        try:
            user = self.env['res.users'].browse(196)
#           user = self.env.user
            authenticated = self.sale_id.authenicate_jwt()
            if not authenticated:
                raise ValidationError(_('Authentication failed: Invalid credentials.'))
            if self.docs_policy == 'in':
                not_all_recipients_signed = self.connector_line_ids.filtered(lambda r: r.send_status and not r.sign_status)
                if not_all_recipients_signed:
                    raise ValidationError(_('Document download failed: Not all recipients have signed the document.'))
                last_recipient = self.connector_line_ids.filtered(lambda r: r.send_status and r.sign_status)
                if not last_recipient:
                    raise ValidationError(_('No recipients available for document download.'))
                line = last_recipient[0]
                if not line.envelope_id:
                    raise ValidationError(_('Document download failed: Missing Docusign envelope.'))

                docu_status, file_content = docu_client.download_documents(user, line.envelope_id)
                if docu_status == 'completed':
                    if isinstance(file_content, str):
                        file_content = file_content.encode('utf-8')

                    attachment = self.env['ir.attachment'].sudo().create({
                        'name': line.name or 'Document',
                        'type': 'binary',
                        'datas': base64.b64encode(file_content).decode('utf-8'),
                        'store_fname': line.name or 'Document.pdf',
                        'mimetype': 'application/pdf',
                        'res_model': 'docusign.connector.lines',
                        'res_id': line.id,
                    })

                    line.sudo().write({
                        'signed_attachment_ids': [(4, attachment.id)],
                        'status': 'completed',
                        'sign_status': True,
                    })
                    next_recipient = self.connector_line_ids.filtered(lambda r: r.id > line.id and not r.send_status)
                    if next_recipient:
                        next_recipient[0].un_signed_attachment_ids |= attachment

            if self.docs_policy == 'out':
                for line in self.connector_line_ids:
                    if not line.envelope_id:
                        raise ValidationError(_('Document download failed: Missing Docusign envelope.'))
                    if line.send_status and not line.sign_status:
                        docu_status, file_content = docu_client.download_documents(user, line.envelope_id)
                        if docu_status == 'completed':
                            if isinstance(file_content, str):
                                file_content = file_content.encode('utf-8')
                            attachment = self.env['ir.attachment'].sudo().create({
                                'name': line.name or 'Document',
                                'type': 'binary',
                                'datas': base64.b64encode(file_content).decode('utf-8'),
                                'store_fname': line.name or 'Document.pdf',
                                'mimetype': 'application/pdf',
                                'res_model': 'docusign.connector.lines',
                                'res_id': line.id,
                            })
                            line.sudo().write({
                                'signed_attachment_ids': [(4, attachment.id)],
                                'status': 'completed',
                                'sign_status': True,
                            })
                            self.env.cr.commit()

            # self.write({'state': 'completed'})
        except Exception as e:
            raise ValidationError(_(str(e)))

    def status_docs(self):
        try:
            user = self.env['res.users'].browse(196)
#           user = self.env.user
            authenticated = self.sale_id.authenicate_jwt()
            for line in self.connector_line_ids:
                if not line.envelope_id:
                    raise ValidationError(_('Action Failed! Docusign envelope is missing.'))
                if line.sign_status != 'completed':
                    docu_status = docu_client.get_status(user, line.envelope_id)
                    if docu_status == 'completed':
                        line.sudo().write({
                            'status': 'completed',
                            'sign_status': True,
                        })
                        self.write({'state': 'completed'})
            if self.state == 'completed':            
                sub = self.env['sale.order'].browse(self.sale_id.id)
                if sub.subscription_state == '1a_pending':
                    self.env['sale.order'].browse(self.sale_id.id).write({'subscription_state': '1b_install'})
                    cm = self.env['contract.management'].sudo().search([('name','=',sub.cabal_sequence)]) 
                    cm[0].write({'state':'signed'})

        except Exception as e:
            raise ValidationError(_(str(e)))