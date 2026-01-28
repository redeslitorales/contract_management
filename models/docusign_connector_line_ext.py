import hashlib
import secrets
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


def _hash_token(token: str) -> str:
    """Return a stable sha256 hash for a token."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


class DocusignConnectorLineMagic(models.Model):
    _inherit = 'docusign.connector.lines'

    recipient_email = fields.Char(
        string='Recipient Email',
        help='Explicit recipient email used for DocuSign. Falls back to partner email when empty.',
    )
    magic_token_hash = fields.Char(string='Magic Link Token Hash', index=True, copy=False)
    magic_token_expires_at = fields.Datetime(string='Magic Link Expires At', copy=False)
    magic_token_used_at = fields.Datetime(string='Magic Link Used At', copy=False)

    @api.constrains('partner_id', 'recipient_email')
    def _check_partner_email(self):
        """Allow placeholder recipient_email when partner email is absent."""
        for line in self:
            candidate = line.recipient_email or line.email or line.partner_id.email
            if not candidate:
                raise ValidationError(
                    _('Recipient %s must have either a partner email or a recipient email to send via DocuSign.')
                    % (line.partner_id.display_name or _('Unknown'))
                )

    def _get_recipient_email(self):
        """Return the email to use for DocuSign, preferring explicit recipient_email."""
        self.ensure_one()
        return (self.recipient_email or self.email or self.partner_id.email or '').strip()

    def generate_magic_link(self, hours_valid: int = 72):
        """Generate and store a one-time magic-link token, returning (token, url)."""
        self.ensure_one()
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        expires_at = fields.Datetime.now() + timedelta(hours=hours_valid)

        self.sudo().write({
            'magic_token_hash': token_hash,
            'magic_token_expires_at': expires_at,
            'magic_token_used_at': False,
        })

        base_url = (self.env['ir.config_parameter'].sudo().get_param('web.base.url') or '').rstrip('/')
        path = f"/contracts/sign/{token}"
        magic_url = f"{base_url}{path}" if base_url else path
        return token, magic_url

    @api.model
    def resolve_magic_token(self, token: str):
        """Return (line, error_code) for a given token, without consuming it."""
        if not token:
            return False, 'missing'
        token_hash = _hash_token(token)
        line = self.sudo().search([('magic_token_hash', '=', token_hash)], limit=1)
        if not line:
            return False, 'not_found'

        now = fields.Datetime.now()
        if line.magic_token_used_at:
            return False, 'used'
        if line.magic_token_expires_at and line.magic_token_expires_at < now:
            return False, 'expired'

        return line, None

    def consume_magic_token(self):
        """Mark the current token as used."""
        self.ensure_one()
        self.sudo().write({'magic_token_used_at': fields.Datetime.now()})
