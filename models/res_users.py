from odoo.exceptions import ValidationError
import logging
from odoo import models, fields, api, exceptions, _
from odoo.http import request
try:
    from docusign_esign import ApiClient
    api_client = ApiClient()
except Exception:
    raise ValidationError(_('Package docusign-esign not found. If you plan to use it, '
                            'please install the docusign-esign library from https://pypi.org/project/docusign-esign/'))
_logger = logging.getLogger(__name__)
import base64, json
from six import PY3, integer_types, iteritems, text_type
import requests
PRIMITIVE_TYPES = (float, bool, bytes, text_type) + integer_types

SCOPES = [
    "signature extended"
]

ADMIN_SCOPES = [
    "signature", "organization_read", "group_read", "permission_read", "user_read", "user_write",
    "account_read", "domain_read", "identity_provider_read", "impersonation"
]

platform_type = {
    'dev': 'account-d.docusign.com',
    'prod': 'account.docusign.com'
}
envelope_events = ["Completed", "Declined", "Delivered", "Sent", "Voided"]


class ResUserCustom(models.Model):
    _inherit = 'res.users'

    record_name = fields.Char(string="Record Name", compute='ds_get_name')
    def ds_get_name(self):
        for rec in self:
            if rec.name:
                rec.record_name =  'DS-Account: ' + rec.name
            else:
                rec.record_name = 'DS-Account'
    # name = fields.Char(string="App Name",)
    code = fields.Char('Code')
    client_id = fields.Char('Integration Key')
    client_secret = fields.Char('Secret Key')
    account_type = fields.Selection([('dev', 'Developer'), ('prod', 'Production')],
                                    default='dev', string='Account Type')


    @api.onchange('account_type')
    def _check_account_type(self):
        for rec in self:
            if not rec.account_type:
                raise ValidationError(_("Account type can't be empty!"))

    login_url = fields.Char('Login URL', compute= '_compute_url')
    redirect_url = fields.Char('Redirect URL', compute= '_get_current_url')
    access_token = fields.Char('Access Token')
    refresh_token = fields.Char('Refresh Token')
    expiry_time = fields.Char('Expires In')
    base_uri = fields.Char('Base URI')
    account_id = fields.Char('Account ID')

    @api.depends('client_id')
    def _get_current_url(self):
        for rec in self:
            base_url = request.httprequest.url_root
            rec.redirect_url = base_url + 'docusign'

    @api.onchange('account_type', 'client_id', 'client_secret')
    def _compute_url(self):
        url_scopes = "+".join(SCOPES)
        for rec in self:
            account_type = rec.account_type if rec.account_type else 'dev'
            api_client.set_oauth_host_name(oauth_host_name=platform_type[account_type])
            rec.login_url = api_client.get_authorization_uri(rec.client_id, SCOPES, rec.redirect_url, 'code')

    def generate_consent_url(self):
        client_id = self.env.user.client_id
        redirect_uri = self.env.user.redirect_url
        base_url = "https://{0}".format(platform_type[self.account_type])
        consent_url = f"{base_url}/oauth/auth?response_type=code&scope=signature%20impersonation&client_id={client_id}&redirect_uri={redirect_uri}"
        return {
            'type': 'ir.actions.act_url',
            'url': consent_url,
            'target': 'new',
        }

    def get_code(self):
        for rec in self:
            if rec.redirect_url and rec.client_id and rec.client_secret:
                return {
                    'name': 'login',
                    'view_id': False,
                    "type": "ir.actions.act_url",
                    'target': '_blank',
                    'url': rec.login_url
                }
            else:
                raise ValidationError('Docusign Credentials are missing. Please ask system admin to add credentials')

    def generate_access_token(self, client_id, client_secret, code):
        url = "https://{0}/oauth/token".format(platform_type[self.account_type])
        integrator_and_secret_key = b"Basic " + base64.b64encode(str.encode("{}:{}".format(client_id, client_secret)))
        headers = {
            "Authorization": integrator_and_secret_key.decode("utf-8"),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        post_params = self.sanitize_for_serialization({
            "grant_type": "authorization_code",
            "code": code
        })
        response = api_client.rest_client.POST(url, headers=headers, post_params=post_params)
        return response

    def get_access_token(self):
        try:
            for rec in self:
                response = self.generate_access_token(rec.client_id, rec.client_secret, rec.code)
                if response.status == 200:
                    data = json.loads(response.data)
                    if 'access_token' in data:
                        self.write({
                            'access_token': data['access_token'],
                            'refresh_token': data['refresh_token'],
                            'expiry_time': data['expires_in']
                        })
                        self.get_user_info()
                return self.action_of_button("Successfully generated the access token!")
        except Exception as e:
            raise ValidationError(_("Not a valid request for access token\nTry again in few seconds "
                                    "after re-trying login with above button."))

    def get_user_info(self):
        try:
            for rec in self:
                url = "https://{0}/oauth/userinfo".format(platform_type[self.account_type])
                headers = {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'Authorization': 'Bearer ' + self.access_token
                }
                response = requests.request("GET", url, headers=headers)
                print(response)
                if response.status_code != 200:
                    raise ValidationError((str(response.text)))
                data = response.json()
                if 'accounts' in data:
                    for account in data['accounts']:
                        self.write({
                            'account_id': account['account_id'],
                            'base_uri': account['base_uri'],
                        })
        except Exception as e:
            raise ValidationError(_("Not a valid request for user info."))

    def sanitize_for_serialization(self, obj):
        PRIMITIVE_TYPES = (float, bool, bytes, text_type) + integer_types
        if obj is None:
            return None
        elif isinstance(obj, PRIMITIVE_TYPES):
            return obj
        elif isinstance(obj, list):
            return [self.sanitize_for_serialization(sub_obj)
                    for sub_obj in obj]
        elif isinstance(obj, tuple):
            return tuple(self.sanitize_for_serialization(sub_obj)
                         for sub_obj in obj)

        if isinstance(obj, dict):
            obj_dict = obj
        else:
            obj_dict = {obj.attribute_map[attr]: getattr(obj, attr)
                        for attr, _ in iteritems(obj.swagger_types)
                        if getattr(obj, attr) is not None}

        return {key: self.sanitize_for_serialization(val)
                for key, val in iteritems(obj_dict)}

    def action_of_button(self, message):
        message_id = self.env['message.wizard'].sudo().create({'message': _(message)})
        return {
            'name': _('Successful'),
            'type': 'ir.actions.act_window',
            'view_mode': 'form',
            'res_model': 'message.wizard',
            'res_id': message_id.id,
            'target': 'new'
        }
    
    def refresh_access_token(self):
        for rec in self:
            url = "https://{0}/oauth/token".format(platform_type[rec.account_type])
            integrator_and_secret_key = b"Basic " + base64.b64encode(str.encode("{}:{}".format(rec.client_id, rec.client_secret)))
            headers = {
                "Authorization": integrator_and_secret_key.decode("utf-8"),
                "Content-Type": "application/x-www-form-urlencoded",
            }
            post_params = {
                "grant_type": "refresh_token",
                "refresh_token": rec.refresh_token
            }
            response = requests.post(url, headers=headers, data=post_params)
            print(response)
            if response.status_code == 200:
                data = response.json()
                rec.write({
                    'access_token': data['access_token'],
                    'refresh_token': data['refresh_token'],
                    'expiry_time': data['expires_in']
                })
                return self.action_of_button("Successfully refreshed the access token!")
            else:
                _logger.error("Failed to refresh access token: %s", response.text)

    def schedule_refresh_token(self):
        cron_name = f'Refresh DocuSign Token for User {self.id}'
        cron = self.env['ir.cron'].search([('name', '=', cron_name)], limit=1)
        if not cron:
            self.env['ir.cron'].create({
                'name': cron_name,
                'model_id': self.env.ref('contract_management.model_res_users').id,
                'state': 'code',
                'code': f'model.browse({self.id}).refresh_access_token()',
                'interval_number': 28,
                'interval_type': 'days',
                'numbercall': -1,
                'doall': False,
            })
        else:
            cron.write({
                'interval_number': 28,
                'interval_type': 'days',
            })