from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
import time, base64, uuid, re, json, jwt, requests


SUBSCRIPTION_DRAFT_STATE = ['1_draft', '1a_pending', '1b_install', '1c_nocontract', '1d_internal', '1e_confirm', '2_renewal']

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

TRANSFER_REASONS = [
        ('sold', 'Transfer of Ownership of Property'),
        ('rental', 'New Renter'),
        ('death', 'Death'),
        ('otro', 'Otro')
    ]

platform_type = {
    'dev': 'account-d.docusign.com',
    'prod': 'account.docusign.com'
}

class SaleTermsConditions(models.Model):
    _name = 'sale.terms.conditions'
    _description = 'Sale Terms and Conditions'

    name = fields.Char(string='Name', required=True, translate=True)
    description = fields.Html(string='Description', translate=True)
    product_category_ids = fields.Many2many('product.category', string='Product Category', required=True)
    is_default = fields.Boolean(string='Default', default=False)

class SaleCoverLetter(models.Model):
    _name = 'sale.cover.letter'
    _description = 'Sale Cover Letter'

    name = fields.Char(string='Name', required=True)
    product_category_id = fields.Many2one('product.category', string='Product Category', required=True)
    cover_letter = fields.Html(string='Cover Letter', translate=True)

class SaleSubscription(models.Model):
    _inherit = 'sale.order'

#    contract_template = fields.Many2one(related='order_line.product_id.categ_id.contract_template', string="Contract Template")
    contract_template = fields.Many2one(
        'ir.actions.report',
        string="Contract Template",
        compute='_compute_contract_template',
        store=True,
    )
    cabal_sequence = fields.Char(string='Contract Number', readonly=True, copy=False)
    contract_send_method = fields.Selection(string='Send Method', selection=CONTRACT_SEND_METHODS, required=True)
    subscription_state = fields.Selection(
        string='Subscription Status',
        selection=SUBSCRIPTION_STATES,
        compute='_compute_subscription_state', store=True, tracking=True, group_expand='_group_expand_states',
    )
    contract_ids = fields.One2many('contract.management', 'subscription_id', string="Contracts")
    docusign_ids = fields.One2many('docusign.connector', 'sale_id', string="DocuSign Envelopes")
    transfer_date = fields.Date(string="Date of Transfer")
    transfer_reason = fields.Selection(string="Transfer Reason", selection=TRANSFER_REASONS)
    previous_partner_id = fields.Many2one('res.partner', string="Previous Client")
    terms_conditions_ids = fields.Many2many('sale.terms.conditions', string='Terms and Conditions')
    cover_letter_id = fields.Many2one('sale.cover.letter', string='Cover Letter', compute='_compute_cover_letter', store=True)
    confirmation_uuid = fields.Char(string='UUID', readonly=True, default=lambda self: str(uuid.uuid4()))
    confirmation_url = fields.Char(string='Confirmation URL', compute='_compute_confirmation_url')
    clause_ids = fields.Many2many('contract.clause', string='Clauses')
    quote_confirmed = fields.Boolean(string='Quote Confirmed', default=False)

    @api.depends('confirmation_uuid')
    def _compute_confirmation_url(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        for order in self:
            order.confirmation_url = f"{base_url}/webhook/confirm_sale_order?uuid={order.confirmation_uuid}"

    @api.depends('order_line.product_id.categ_id')
    def _compute_cover_letter(self):
        for order in self:
            categories = order.order_line.mapped('product_id.categ_id')
            if categories:
                cover_letter = self.env['sale.cover.letter'].sudo().search([('product_category_id', 'in', categories.ids)], limit=1)
                order.cover_letter_id = cover_letter
            else:
                order.cover_letter_id = False

    @api.depends(
        'order_line.price_total',
        'order_line.product_id',
        'order_line.product_id.categ_id',
        'order_line.product_id.categ_id.contract_template',
    )
    def _compute_contract_template(self):
        for order in self:
            # Consider only real product lines that have a contract_template
            lines = order.order_line.filtered(
                lambda l: not l.display_type
                and l.product_id
                and l.product_id.categ_id.contract_template
            )

            if not lines:
                order.contract_template = False
                continue

            # Choose the line with the highest cost (price_total = unit * qty)
            main_line = max(lines, key=lambda l: l.price_total)

            order.contract_template = main_line.product_id.categ_id.contract_template

    @api.onchange('order_line')
    def _onchange_order_line(self):
        for order in self:
            terms_conditions = self.env['sale.terms.conditions']
            for line in order.order_line:
                product_categories = line.product_id.categ_id
                terms_conditions |= self.env['sale.terms.conditions'].sudo().search([('product_category_ids', 'in', product_categories.ids), ('is_default', '=', True)])
            order.terms_conditions_ids = [(6, 0, terms_conditions.ids)]
            language = order.partner_id.lang or 'en_US'
            order.clause_ids = self.env['contract.clause'].get_applicable_clauses(order.contract_template.id)

    @api.model
    def _get_cabal_sequence(self):
        return self.env['ir.sequence'].sudo().next_by_code('sus.contract.cabal')
    
    def get_confirmation_url(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        return f"{base_url}/confirm_order/{self.confirmation_uuid}"
    
    def move_to_in_progress(self, records):
        # This method is being used to correct the status of the subscription to In Progress when a subscription is in Pending Install and the ONU has been set.
        for record in records:
            record.subscription_state = '3_progress'

    def move_to_next_stage(self, event):
        raise ValidationError(event)
        # When contract is completed, move to Pending Install unless status is already In Progress
        if event == 'Contract Complete':
            if self.subscription_state not in  ['3_progress']:
                self.write({'subscription_state': '1d_internal'})
        return

    def signed_manually(self):
        if self.subscription_state in ['1a_pending'] and self.contract_send_method == 'physical':
            self.write({'subscription_state': '1b_install'})
        else:
            raise UserError('Error: No esta firmado fisicamente.')

    def return_to_progress(self):
        if self.subscription_state in ['1d_internal', '1b_install'] and (self.invoice_ids or self.origin_order_id) :
            self.write({'subscription_state': '3_progress'})
    
#    def generate_cover_letter(self):
#        for order in self:
#            cover_letter_template = self.env.ref('your_module.cover_letter_template')
#            cover_letter_html = cover_letter_template._render({
#                'doc': order,
#            }, engine='ir.qweb')
#            order.cover_letter_id.cover_letter = cover_letter_html

    def manually_signed(self):
        self.write({'state': 'sale', 'subscription_state': '1b_install'})

    def authenicate_jwt(self):
        # Create the JWT assertion
        user = self.env['res.users'].browse(196)
        now = int(time.time())
        payload = {
            'iss': self.env['ir.config_parameter'].sudo().get_param('docusign_client_id', ''),
            'sub': self.env['ir.config_parameter'].sudo().get_param('docusign_user_id', ''),
            'aud': platform_type[user.account_type],
            'iat': now,
            'exp': now + 3600,
            'scope': 'signature impersonation'
        }
        jwt_assertion = jwt.encode(payload, self.env['ir.config_parameter'].sudo().get_param('docusign_private_key', ''), algorithm='RS256')
        # Request an access token
        url = "https://{0}/oauth/token".format(platform_type[user.account_type])
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        data = {
            'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
            'assertion': jwt_assertion
        }
        response = requests.post(url, headers=headers, data=data)
        access_token = response.json().get('access_token')
        user.access_token = access_token

        if not access_token:
            raise ValidationError(_("Failed to obtain access token from DocuSign"))
        
        return True
                
    # Method to be used in case a contract needs to be transferred
    def action_subscription_transfer_wizard(self):
        if not self:
            raise ValueError("Expected singleton: sale.order()")
        self.ensure_one()
        if self.is_subscription:
            # Retrieve DocuSign credentials from the custom model
            user = self.env.user
            if not user.access_token or not user.account_id:
                raise UserError("DocuSign credentials are not active.  Please sign in and get your access token.")
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'action_subscription_transfer_wizard',
            'view_mode': 'form',
            'target': 'new'
        }

    # Method to be used in case a contract needs to be sentm, but the contract is in confirmed status
    def action_open_contract_send_method_wizard(self):
        self.ensure_one()
        if self.subscription_id.contract_ids:
            raise UserError("The contract has already been sent via Docusign.  Please review.")
        if self.is_subscription:
            # Retrieve DocuSign credentials from the custom model
            user = self.env['res.users'].browse(196)
#           user = self.env.user
            if not user.access_token or not user.account_id:
                raise UserError("DocuSign credentials are not active.  Please sign in and get your access token.")
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'contract.send.method.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_send_method': self.contract_send_method}
        }

    def action_confirm_via_uuid(self):
        # Set use to contratos@cabal.sv
        user = self.env['res.users'].browse(196)
        authenticated = self.authenicate_jwt()
        if authenticated:
            res = super(SaleSubscription, self).action_confirm()
            if self.is_subscription:
                self.write({'subscription_state': '1c_nocontract'})
            if self.contract_send_method == 'whatsapp' and not self.partner_id.whatsapp:
                self.write({'contract_send_method': 'email'})
            else:
                if self.partner_id.whatsapp.startswith('+1') and len(self.partner_id.whatsapp) == 12:
                    match = re.match(r'^\+(\d{1})(\d{10})$', self.partner_id.whatsapp)
                elif self.partner_id.whatsapp.startswith('+503') and len(self.partner_id.whatsapp) == 12:
                    match = re.match(r'^\+(\d{1,3})(\d+)$', self.partner_id.whatsapp)
                else:
                    match = re.match(r'^\+(\d{1,3})(\d{4,14})$', self.partner_id.whatsapp)
                if match:
                    country_code = match.group(1)
                    phone_number = match.group(2)
                else:
                    self.write({'contract_send_method': 'email'})
            self.action_send_for_signature()
            return res
        
    def action_confirm(self):
        user = self.env['res.users'].browse(196)
#       user = self.env.user
        authenticated = self.authenicate_jwt()
        if authenticated:
            res = super(SaleSubscription, self).action_confirm()
            if self.is_subscription:
                self.write({'subscription_state': '1c_nocontract'})
                # Retrieve DocuSign credentials from the custom model
                if not user.access_token or not user.account_id:
                    raise UserError("DocuSign credentials are not active.  Please sign in and get your access token.")
                return {
                    'type': 'ir.actions.act_window',
                    'res_model': 'contract.send.method.wizard',
                    'view_mode': 'form',
                    'target': 'new',
                    'context': {'default_send_method': self.contract_send_method}
                }
            return res
        else:
            consent_action = user.generate_consent_url(self)
            if consent_action:
                raise UserError('You are now authenticated.  Please try again.')
            else:
                raise ValidationError(_("Failed to obtain access token from DocuSign"))


    def action_send_for_signature(self):
        for contract in self:
            # Step 1: Generate contract number
            if contract.is_subscription and not contract.cabal_sequence:
                contract.cabal_sequence = contract._get_cabal_sequence()
            # Step2 - Fetch the contract template
            if not contract.contract_template:
                raise UserError('Contract template not specified.')
            # Step 3: Create the document to be signed using the template
            document = self._create_document_to_be_signed(contract, contract.contract_template)
            if contract.contract_send_method != 'physical':
                # Step 4: Create the connector and connector line records
                connector_id = self._send_document_to_docusign(contract, document)
                # Send document from Docusign
                send_contract_result = connector_id.send_docs(contract.contract_send_method)
                msg_text = "sent to customer."
                if send_contract_result['name'] == "Successful":
                    contract.message_post(body=f'SUCCESS: Contract {document.name} '+msg_text, attachment_ids=[document.id])
                    contract.write({'state': 'sale', 'subscription_state': '1a_pending'})
                else:
                    raise ValidationError(str(send_contract_result))
            else:
                    contract.message_post(body=f'Contract {document.name} is ready to be printed and signed.', attachment_ids=[document.id])
                    contract.write({'state': 'sale', 'subscription_state': '1a_pending'})
                    contract.create_print_sign_activity()


            # Step 5 Create the contract management record
            k_management = self.env['contract.management'].create({
                'subscription_id': contract.id,
                "contract_send_method": contract.contract_send_method,
                'state': 'signature_in_process',
            })
            if contract.contract_send_method != 'physical':
                k_management.write({'docusign_id': connector_id.id})
    #            contract.write({'contract_management_id': k_management.id})
                # Update the contract with the DocuSign envelope ID
    #            contract.docusign_envelope_id = envelope_id
    #            contract.state = 'signature_in_process'
        return

    def _create_document_to_be_signed(self, subscription, report_template):
        # Render the report as PDF
        # Check to make sure that the contract template has been specified
        if not report_template:
            raise ValueError("No contract template specified.")
        # Fetch report action
        report_action = report_template.sudo().read()[0]
        # Generate the attachment
        pdf_content, _ = self.env['ir.actions.report']._render_qweb_pdf(report_action['id'], [subscription.id])
        # Create an attachment for the generated PDF
        attachment = self.env['ir.attachment'].create({
            'name': f'{subscription.cabal_sequence}_{subscription.name}_customer_contract.pdf',
            'type': 'binary',
            'datas': base64.b64encode(pdf_content),
            'res_model': 'sale.order',
            'res_id': subscription.id,
            'mimetype': 'application/pdf'
        })
        return attachment

    def _send_document_to_docusign(self, contract, document):
        # Retrieve DocuSign credentials from the custom model
        user = self.env['res.users'].browse(196)
        if not user.access_token or not user.account_id:
            raise UserError("DocuSign credentials are not configured.")
        connector_record = self.env['docusign.connector'].create({
            'name': contract.cabal_sequence,
            'responsible_id': user.id,
            'state': 'new',
            'send_docs_in_hierarchy': True,
            'docs_policy': 'in',
            'model': 'sale',
            'sale_id': contract.id,
            'attachment_ids': [(6, 0, [document.id])]
        })
        connector_line_record = self.env['docusign.connector.lines'].create({
            'partner_id': contract.partner_id.id,
            'email': contract.partner_id.email_normalized,
            'status': 'draft',
            'un_signed_attachment_ids':  [(6, 0, [document.id])],
            'record_id': connector_record.id,
            'name': document.name
        })
        return connector_record

    def create_print_sign_activity(self):
        for subscription in self:
            activity_type = self.env.ref('mail.mail_activity_data_todo').id
            user_id = subscription.create_uid.id
            summary = 'Print and Sign Contract'
            note = 'Please print and sign the contract.'

            self.env['mail.activity'].create({
                'activity_type_id': activity_type,
                'res_model_id': self.env['ir.model']._get('sale.order').id,
                'res_id': subscription.id,
                'user_id': user_id,
                'date_deadline': date.today(),
                'summary': summary,
                'note': note,
            })

    def action_open_contract_upload_wizard(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Upload Contract',
            'res_model': 'contract.upload.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_subscription_id': self.id,
            }
        }
    def send_quote_via_whatsapp(self, records):

        auth_token = self.env['ir.config_parameter'].get_param('fc_auth_token', '')
        fc_url_base = self.env['ir.config_parameter'].get_param('fc_url_base', '')
        fc_url_send = self.env['ir.config_parameter'].get_param('fc_url_send', '')
        fc_url_verify = self.env['ir.config_parameter'].get_param('fc_url_verify', '')
        namespace = self.env['ir.config_parameter'].get_param('wa_namespace', '')
        logo = self.env['ir.config_parameter'].get_param('wa_logo_file', '')
        message_template = "confirmacion_de_orden"

        for rec in self:
            
            if rec.partner_id.whatsapp:
                client_phone = rec.partner_id.whatsapp
                
                # Generate the PDF quote
                attachment = self.env['ir.attachment'].search([('res_model', '=', 'sale.order'), ('res_id', '=', rec.id), ('mimetype', '=', 'application/pdf')], limit=1)
                pdf_url = '/web/content/%s?download=true' % (attachment.id)
                pdf_base64 = base64.b64encode(pdf_content).decode('utf-8')

                headers = {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'Authorization': f"Bearer {auth_token}"
                }
                payload = {
                    "to": client_phone,
                    "type": "template",
                    "template": {
                        "namespace": namespace,
                        "name": message_template,
                        "language": {
                            "policy": "deterministic",
                            "code": "en"
                        },
                        "components": [
                            {
                                "type": "header",
                                "parameters": [
                                    {
                                        "type": "document",
                                        "document": {
                                            "link": f"data:application/pdf;base64,{pdf_base64}",
                                            "filename": f"{rec.name}.pdf"
                                        }
                                    }
                                ]
                            },
                            {
                                "type": "body",
                                "parameters": [
                                    {
                                        "type": "text",
                                        "text": rec.partner_id.name
                                    }
                                ]
                            },
                            {
                                "type": "button",
                                "sub_type": "url",
                                "index": "0",
                                "parameters": [
                                    {
                                        "type": "text",
                                        "text": rec.confirmation_url
                                    }
                                ]
                            }
                        ]
                    }
                }
                #               payload = '{ "from": { "phone_number": "+50379401214" }, "provider": "whatsapp", "to": [ { "phone_number": "'+str(client_phone)+'" } ], "data": { "message_template": { "storage": "conversation", "template_name": "'+message_template+'", "namespace": "'+namespace+'", "language": { "policy": "deterministic", "code": "'+lang_code+'" }, "rich_template_data": { "header": { "type": "document", "document":{ "link": "'+link+'", "filename": f"{rec.name}.pdf"} }, "body": { "params": [ {"data": "'+str(rec.partner_id.name)+'"} ] }, "button": {"sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": "'+str(rec.confirmation_url)+'&send_method=whatapp'"}] } } } } }"
                wa_sent = requests.post(fc_url_base+'/'+fc_url_send, headers=headers, data=payload)
                response = wa_sent.json()
                if wa_sent.status_code == 202:
                    time.sleep(2.5)
                    wa_verify = requests.get(fc_url_base+fc_url_verify+str(response['request_id']),headers=headers)
                    response_ver = wa_verify.json()
                    self.message_post(body="Notificacion por WhatsApp "+str(response_ver['outbound_messages'][0]['status']).title()+" con request ID: "+str(response['request_id']))
                else:
                    self.message_post(body="Notificacion por WhatsApp FALLIDA con codigo "+str(wa_sent.status_code))
                    helpdesk_ticket = self.env['helpdesk.ticket'].sudo().create({
                        'name': "Unable to Send WhatsApp",
                        'description': "WhatsApp Notification to "+str(client_phone)+" was "+str(response),
                        'message_needaction': True, 'ticket_type_id': 4
                        })

            else:
                raise ValidationError("Cliente no tiene numero de WhatsApp registrado")

            return response

class ContractSendMethodWizard(models.TransientModel):
    _name = 'contract.send.method.wizard'
    _description = 'Contract Send Method Wizard'

    send_method = fields.Selection(string='Send Method', selection=CONTRACT_SEND_METHODS, required=True)

    def action_confirm_send_method(self):
        self.ensure_one()
        contract_id = self.env.context.get('active_id')
        if not contract_id:
            raise UserError("No active contract found.")
        contract = self.env['sale.order'].browse(contract_id)

        # Validation for WhatsApp send method
        if self.send_method == 'whatsapp' and not contract.partner_id.whatsapp:
            raise ValidationError("The customer does not have a valid WhatsApp number.")
        else:
            match = re.match(r'^\+(\d{1,3})(\d+)$', contract.partner_id.whatsapp)
            if match:
                country_code = match.group(1)
                phone_number = match.group(2)
            else:
                raise ValidationError("The customer does not have a valid WhatsApp number.")
        contract.contract_send_method = self.send_method
        if self.send_method != 'donotsend':
            return contract.action_send_for_signature()
        else:
            raise UserError('Contract NOT SENT!')

class SubscriptionTransferWizard(models.TransientModel):
    _name = 'subscription.transfer.wizard'
    _description = 'Subscription Transfer Wizard'

    subscription_id = fields.Many2one('sale.order', string='Subscription', required=True)
    new_customer_id = fields.Many2one('res.partner', string='New Customer', required=True)
    transfer_date = fields.Date(string="Effective Date of Transfer")
    transfer_reason = fields.Selection(string="Transfer Reason", selection=[
        ('sold', 'Transfer of Ownership of Property'),
        ('rental', 'New Renter'),
        ('death', 'Death'),
        ('otro', 'Otro')
    ])
    contract_option = fields.Selection([
        ('remaining_period', 'Remaining Period'),
        ('standard_period', 'Standard Contract Period')
    ], string='Contract Option', required=True, default='standard_period')

    @api.onchange('transfer_reason')
    def _onchange_transfer_reason(self):
        if self.transfer_reason == 'death':
            self.contract_option = 'remaining_period'
        else:
            self.contract_option = False

    def transfer_subscription(self):
        self.ensure_one()
        subscription = self.subscription_id
        new_customer = self.new_customer_id
        if not subscription or not new_customer:
            raise UserError('Please select a subscription and a new customer.')
        subscription.previous_partner_id = subscription.partner_id
        subscription.partner_id = new_customer.id
        subscription.transfer_date = self.transfer_date
        subscription.transfer_reason = self.transfer_reason
        subscription.action_open_contract_send_method_wizard()
        if self.contract_option == 'remaining_period':
            start_date = date.today()
            end_date = subscription.contract_ids.end_date
        else:
            start_date = date.today()
            contract_term = subscription.contract_term.term
            end_date = start_date + relativedelta(months=contract_term)
        self.env['mail.message'].create({
            'body': f'Subscription transferred on {subscription.transfer_date} from {subscription.previous_partner_id.name} to {new_customer.name} for {subscription.transfer_reason}.',
            'model': 'sale.order',
            'res_id': subscription.id,
            'message_type': 'notification',
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }
    
class ContractUploadWizard(models.TransientModel):
    _name = 'contract.upload.wizard'
    _description = 'Contract Upload Wizard'

    contract_file = fields.Binary(string='Contract File', required=True)
    contract_filename = fields.Char(string='Contract Filename')
    subscription_id = fields.Many2one('sale.order', string='Subscription', required=True, default=lambda self: self._default_subscription_id())

    @api.model
    def _default_subscription_id(self):
        return self.env.context.get('default_subscription_id')

    def upload_contract(self):
        self.ensure_one()
        if not self.contract_file:
            raise UserError('Please upload a contract file.')

        # Create a record in the contract management module
        contract = self.env['contract.management'].create({
            'name': self.contract_filename,
            'subscription_id': self.subscription_id.id,
            'contract_file': self.contract_file,

        })

        # Change the subscription status to 1b_install
        self.subscription_id.write({'subscription_state': '1b_install'})

        # Store the contract document in the documents tab of the relevant subscription
        attachment = self.env['ir.attachment'].create({
            'name': self.contract_filename,
            'type': 'binary',
            'datas': self.contract_file,
            'res_model': 'sale.order',
            'res_id': self.subscription_id.id,
        })

        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }