from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
import time
import base64
import requests
import json
import re
import logging
from docusign_esign import ApiClient, EnvelopesApi, OAuth, Signer, RecipientPhoneNumber, Tabs, SignHere
from odoo.addons.odoo_docusign.models import docu_client

_logger = logging.getLogger(__name__)

platform_type = {
    'dev': 'account-d.docusign.com',
    'prod': 'account.docusign.com'
}

class OverrideDocumentStatus(models.Model):
    _inherit = 'docusign.connector'

    state = fields.Selection([('new', 'New'), ('open', 'Open'),('sent', 'Sent'), ('customer', 'Customer Signed'), ('completed', 'Completed')], default='new')
    monthly_payment = fields.Float(string='Monthly Payment', help='Total of recurring line items with taxes')
    contract_value = fields.Float(string='Contract Value', help='Monthly payment * contract length')

    def send_docs(self, send_method):
        try:
            user = self.env['res.users'].browse(196)
#           user = self.env.user
            if not self.attachment_ids:
                raise ValidationError(_('Attachment(s) not found.'))
            if not self.connector_line_ids:
                raise ValidationError(_("No recipient(s) found for this record."))

            company_email = self.env['res.users'].sudo().browse(196).email
            company_name = self.env['res.users'].sudo().browse(196).name
            
            _logger.info("[DocuSign Send] send_method=%s", send_method)
            
            if self.docs_policy == 'in':
                # Check if this is the first send (no lines have envelope_id yet)
                lines_with_envelope = self.connector_line_ids.filtered(lambda l: l.envelope_id)
                
                if not lines_with_envelope:
                    # First send - create envelope with ALL signers at once
                    _logger.info("[DocuSign Send] First send - creating envelope with %d signers", len(self.connector_line_ids))
                    
                    # Get the attachment
                    attach_file = self.attachment_ids[0]
                    attach_file_name = attach_file.name
                    attach_file_data = attach_file.sudo().read(['datas'])
                    file_data_encoded_string = attach_file_data[0]['datas']
                    
                    # Build signers list from all connector lines
                    signers_list = []
                    for idx, line in enumerate(self.connector_line_ids.sorted(key=lambda l: l.id), 1):
                        # All signers need email for identity
                        if not line.email:
                            raise ValidationError(_(f"Email not set for recipient: {line.partner_id.name}"))
                        
                        # First signer uses the send_method from wizard, others always use email
                        current_send_method = send_method
                        if idx != 1:
                            current_send_method = 'email'  # Company signer always email delivery

                        phone_obj = None
                        country_code = None
                        number = None

                        if current_send_method.lower() == 'whatsapp':
                            current_send_method = 'WhatsApp'
                            if not line.partner_id.whatsapp:
                                raise ValidationError(_(
                                    f"WhatsApp number not set for customer: {line.partner_id.name}"
                                ))

                            phone_cleaned = line.partner_id.whatsapp.lstrip('+')

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
                                # fallback - you may want to improve this
                                country_code = phone_cleaned[:3]
                                number = phone_cleaned[3:]

                            phone_obj = RecipientPhoneNumber(
                                country_code=country_code,
                                number=number
                            )

                        signer = Signer(
                            name=line.partner_id.name,
                            email=line.email,
                            recipient_id=str(idx),
                            routing_order=str(idx),
                            delivery_method=current_send_method,
                            **({"phone_number": phone_obj} if phone_obj else {})
                        )

                        signer.partner = line.partner_id
                        signers_list.append(signer)

                        if send_method == "whatsapp":
                            _logger.info(
                                "[DocuSign Send] Signer %d (WhatsApp): %s (%s, +%s %s)",
                                idx, line.partner_id.name, line.email, country_code, number
                            )
                        else:
                            _logger.info(
                                "[DocuSign Send] Signer %d (Email): %s (%s)",
                                idx, line.partner_id.name, line.email
                            )
                   
                    # Prepare custom fields for DocuSign envelope
                    custom_fields = None
                    if self.monthly_payment or self.contract_value:
                        custom_fields = {
                            'textCustomFields': []
                        }
                        if self.monthly_payment:
                            custom_fields['textCustomFields'].append({
                                'name': 'monthly_payment',
                                'value': str(round(self.monthly_payment, 2)),
                                'show': 'true'
                            })
                        if self.contract_value:
                            custom_fields['textCustomFields'].append({
                                'name': 'contract_value',
                                'value': str(round(self.contract_value, 2)),
                                'show': 'true'
                            })
                    
                    # Send envelope with all signers and custom fields
                    envelope_id = docu_client.send_docusign_envelope_multiple_signers(
                        self.env, user, attach_file_name, file_data_encoded_string, signers_list,
                        custom_fields=custom_fields
                    )
                    
                    # Set the SAME envelope_id on ALL connector lines
                    for line in self.connector_line_ids:
                        line.un_signed_attachment_ids |= attach_file
                        line.sudo().write({
                            'status': 'sent',
                            'name': attach_file.name,
                            'envelope_id': envelope_id,  # Same envelope ID for all!
                            'send_status': True,
                            'recipient_id': str(idx),
                        })
                        _logger.info("[DocuSign Send] Set envelope_id=%s on line %d (%s)",
                                    envelope_id, line.id, line.email)
                    
                    self.write({'state': 'sent'})
                    self.env.cr.commit()
                    return self.action_of_button(_("Document sent to %d recipients") % len(signers_list))
                else:
                    # Envelope already sent
                    raise ValidationError(_("Document has already been sent to DocuSign."))

        except Exception as e:
            raise ValidationError(_(str(e)))

    def download_docs(self):
        try:
            _logger.info("[DocuSign Download] Starting download for connector %s", self.id)
            
            # Authenticate and get the user with fresh token (hardcoded user 196 for consistency)
            authenticated = self.sale_id.authenicate_jwt()
            user = self.env['res.users'].browse(196)
            
            if not authenticated:
                _logger.error("[DocuSign Download] Authentication failed")
                raise ValidationError(_('Authentication failed: Invalid credentials.'))
            
            _logger.info("[DocuSign Download] Docs policy: %s", self.docs_policy)
            
            if self.docs_policy == 'in':
                last_recipient = self.connector_line_ids.filtered(lambda r: r.send_status and r.sign_status)
                if not last_recipient:
                    _logger.error("[DocuSign Download] No signed recipients found")
                    raise ValidationError(_('No recipients available for document download.'))
                
                line = last_recipient[0]
                if not line.envelope_id:
                    _logger.error("[DocuSign Download] Missing envelope_id for line %s", line.id)
                    raise ValidationError(_('Document download failed: Missing Docusign envelope.'))

                _logger.info("[DocuSign Download] Downloading envelope %s", line.envelope_id)
                docu_status, document_data = docu_client.download_documents(self.env, user, line.envelope_id)
                _logger.info("[DocuSign Download] Status: %s, Document data: %s", 
                           docu_status, "received" if document_data else "None")
                
                # Validate document_data
                if not document_data:
                    _logger.error("[DocuSign Download] No document data received from DocuSign (status: %s)", docu_status)
                    raise ValidationError(_('Document download failed: No content received from DocuSign API. Status: %s') % docu_status)
                
                # Extract content from the document data dictionary
                file_content = document_data.get('content')
                filename = document_data.get('filename', line.name or 'Document.pdf')
                mimetype = document_data.get('mimetype', 'application/pdf')
                
                if not file_content:
                    _logger.error("[DocuSign Download] Document data missing 'content' field")
                    raise ValidationError(_('Document download failed: Document data is incomplete.'))
                
                # Check if all recipients are completed (docu_status can be a dict or string)
                all_completed = False
                if isinstance(docu_status, dict):
                    # If it's a dict, check that all values are 'completed'
                    all_completed = all(status == 'completed' for status in docu_status.values())
                    _logger.info("[DocuSign Download] All recipients completed: %s", all_completed)
                elif docu_status == 'completed':
                    all_completed = True
                
                if not all_completed:
                    _logger.error("[DocuSign Download] Document status is not completed: %s", docu_status)
                    raise ValidationError(_('Document download failed: Document status is %s') % str(docu_status))
                
                if all_completed:
                    # file_content is already bytes from docu_client, no need to encode
                    _logger.info("[DocuSign Download] Processing completed document: %s (%d bytes)", 
                               filename, len(file_content))

                    # Find the contract.management record linked to this connector
                    contract_mgmt = self.env['contract.management'].search([('docusign_id', '=', self.id)], limit=1)
                    
                    if not contract_mgmt:
                        _logger.warning("[DocuSign Download] No contract.management found for connector %s", self.id)
                        # Fallback to storing on connector line
                        res_model = 'docusign.connector.lines'
                        res_id = line.id
                    else:
                        _logger.info("[DocuSign Download] Storing signed document on contract.management %s", contract_mgmt.id)
                        res_model = 'contract.management'
                        res_id = contract_mgmt.id

                    attachment = self.env['ir.attachment'].sudo().create({
                        'name': filename,
                        'type': 'binary',
                        'datas': base64.b64encode(file_content).decode('utf-8'),
                        'store_fname': filename,
                        'mimetype': mimetype,
                        'res_model': res_model,
                        'res_id': res_id,
                    })
                    _logger.info("[DocuSign Download] Created attachment %s on %s (ID: %s)", 
                               attachment.id, res_model, res_id)

                    line.sudo().write({
                        'signed_attachment_ids': [(4, attachment.id)],
                        'status': 'completed',
                        'sign_status': True,
                    })
                    next_recipient = self.connector_line_ids.filtered(lambda r: r.id > line.id and not r.send_status)
                    if next_recipient:
                        next_recipient[0].un_signed_attachment_ids |= attachment
                    
                    _logger.info("[DocuSign Download] Successfully downloaded signed document")
                    
                    return {
                        'type': 'ir.actions.client',
                        'tag': 'display_notification',
                        'params': {
                            'title': _('Download Complete'),
                            'message': _('Signed document downloaded successfully.'),
                            'type': 'success',
                            'sticky': False,
                        }
                    }
                else:
                    _logger.warning("[DocuSign Download] Status is not completed: %s", docu_status)
                    raise ValidationError(_('Document download failed: Document status is %s') % docu_status)

            # self.write({'state': 'completed'})
        except Exception as e:
            _logger.exception("[DocuSign Download] Error: %s", str(e))
            raise ValidationError(_(str(e)))

    def status_docs(self):
        """Override to use contract_management's legacy docu_client and add completion handling."""
        
        try:
            # Authenticate and get the user with fresh token (hardcoded user 196 like legacy code)
            authenticated = self.sale_id.authenicate_jwt()
            user = self.env['res.users'].browse(196)
            
            _logger.info("[DocuSign Status Check] Starting for connector %s - Total lines: %s", 
                        self.id, len(self.connector_line_ids))
            
            for line in self.connector_line_ids:
                if not line.envelope_id:
                    raise ValidationError(_('Action Failed! Docusign envelope is missing.'))
                
                _logger.info("[DocuSign Status Check] Line %s - Partner: %s (%s), Envelope: %s, Current sign_status: %s",
                            line.id, line.partner_id.name, line.email, line.envelope_id, line.sign_status)
                
                if not line.sign_status:
                    docu_status = docu_client.get_status(self.env, user, line.envelope_id)
                    _logger.info("[DocuSign Status Check] Line %s - DocuSign returned status: %s", 
                                line.id, docu_status)
                    
                    # Handle dict response (multiple signers) or string response (single signer)
                    line_status = 'unknown'
                    if isinstance(docu_status, dict):
                        # Multiple signers - look up this line's email
                        line_email = line.email.lower() if line.email else ''
                        line_status = docu_status.get(line_email, 'unknown')
                        _logger.info("[DocuSign Status Check] Line %s - Email %s has status: %s", 
                                    line.id, line_email, line_status)
                    else:
                        # Single signer - use status directly
                        line_status = docu_status
                    
                    if line_status == 'completed':
                        line.sudo().write({
                            'status': 'completed',
                            'sign_status': True,
                        })
                        self.message_post(
                            body=_('Document signed by %s (verified via status check)') % line.partner_id.name,
                            subject=_('Signature Confirmed'),
                            message_type='notification',
                            subtype_xmlid='mail.mt_note'
                        )
            
            # Contract-specific logic: Update state based on signature progress
            signed_lines = [l for l in self.connector_line_ids if l.sign_status]
            total_lines = len(self.connector_line_ids)
            _logger.info("[DocuSign Status Check] Connector %s - Signed: %s/%s lines", 
                        self.id, len(signed_lines), total_lines)
            
            all_signed = all(l.sign_status for l in self.connector_line_ids)
            any_signed = any(l.sign_status for l in self.connector_line_ids)
            
            _logger.info("[DocuSign Status Check] Current state: %s, any_signed: %s, all_signed: %s", 
                        self.state, any_signed, all_signed)
            
            if all_signed:
                _logger.info("[DocuSign Status Check] ALL LINES SIGNED - Marking connector %s as completed", self.id)
                self.write({'state': 'completed'})
            elif any_signed:
                _logger.info("[DocuSign Status Check] SOME LINES SIGNED - Marking connector %s as 'customer' (Customer Signed)", self.id)
                self.write({'state': 'customer'})
            elif not all_signed:
                _logger.info("[DocuSign Status Check] NOT all lines signed yet - Connector %s remains in state: %s", 
                            self.id, self.state)
            
            # If connector completed, update subscription and contract management states
            if self.state == 'customer':
                # Customer signed - awaiting Cabal signature
                sub = self.env['sale.order'].browse(self.sale_id.id)
                if sub.subscription_state == '1a_pending':
                    sub.write({'subscription_state': '1d_internal'})
                    _logger.info("[DocuSign Status Check] Subscription %s updated to 1d_internal (Pending Cabal Signature)", sub.id)
            elif self.state == 'completed':
                # All signatures complete - ready for install
                sub = self.env['sale.order'].browse(self.sale_id.id)
                if sub.subscription_state in ['1a_pending', '1d_internal']:
                    sub.write({'subscription_state': '1b_install'})
                    cm = self.env['contract.management'].sudo().search([('name','=',sub.cabal_sequence)]) 
                    if cm:
                        cm[0].write({'state':'signed'})
                    _logger.info("[DocuSign Status Check] Subscription %s updated to 1b_install (Pending Install)", sub.id)
            
            # Return notification instead of popup
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Status Check Complete'),
                    'message': _('Signature status updated. %s of %s recipients have signed.') % (len(signed_lines), total_lines),
                    'type': 'success',
                    'sticky': False,
                }
            }

        except Exception as e:
            raise ValidationError(_(str(e)))