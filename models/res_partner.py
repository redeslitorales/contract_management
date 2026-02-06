import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.osv import expression

from .email_domain_utils import (
    DEFAULT_BAD_EMAIL_DOMAIN_MAP,
    normalize_email_domain,
    parse_bad_email_domain_map,
)


_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    BAD_EMAIL_DOMAIN_PARAM_KEY = 'contract_management.bad_email_domain_map'

    def action_open_change_payment_day_batch_wizard(self):
        self.ensure_one()
        partner = self.commercial_partner_id or self
        active_states = ['3_progress', '4_paused', '5_renewed']
        subscriptions = self.env['sale.order'].search([
            ('partner_id.commercial_partner_id', '=', partner.id),
            ('is_subscription', '=', True),
            ('subscription_state', 'in', active_states),
        ])
        candidate_dates = [d for d in subscriptions.mapped('next_invoice_date') if d]
        base_date = min(candidate_dates) if candidate_dates else fields.Date.context_today(self)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Align Payment Day (All Subs)'),
            'res_model': 'change.payment.date.batch.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_partner_id': partner.id,
                'default_payment_day': base_date.day,
            },
        }

    # Helpers for email domain hygiene
    def _get_bad_email_domain_map(self):
        raw = self.env['ir.config_parameter'].sudo().get_param(self.BAD_EMAIL_DOMAIN_PARAM_KEY, '')
        mapping = parse_bad_email_domain_map(raw)
        return mapping or DEFAULT_BAD_EMAIL_DOMAIN_MAP

    def _validate_email_domain(self, vals):
        if 'email' not in vals:
            return
        email_val = vals.get('email')
        if not email_val:
            return
        corrected = normalize_email_domain(email_val, self._get_bad_email_domain_map())
        if corrected and corrected != email_val:
            raise UserError(_("The email domain looks incorrect. Did you mean %s?") % corrected)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._validate_email_domain(vals)
        return super().create(vals_list)

    def write(self, vals):
        self._validate_email_domain(vals)
        return super().write(vals)

    @api.model
    def fix_bad_email_domains(self, limit=500, dry_run=False):
        mapping = self._get_bad_email_domain_map()
        if not mapping:
            return {'dry_run': dry_run, 'found': 0, 'to_fix': 0, 'sample': []}

        # Build OR domain to avoid scanning all partners.
        domains = [[('email', 'ilike', '@%s' % bad)] for bad in mapping]
        if not domains:
            return {'dry_run': dry_run, 'found': 0, 'to_fix': 0, 'sample': []}
        search_domain = expression.OR(domains) if len(domains) > 1 else domains[0]

        partners = self.sudo().search(search_domain, limit=limit)
        changes = []
        for partner in partners:
            new_email = normalize_email_domain(partner.email, mapping)
            if new_email and new_email != partner.email:
                changes.append((partner.id, partner.email, new_email))
                if not dry_run:
                    partner.write({'email': new_email})

        if not dry_run and changes:
            _logger.info("Fixed bad email domains for %s partners (limit=%s)", len(changes), limit)

        return {
            'dry_run': dry_run,
            'found': len(partners),
            'to_fix': len(changes),
            'sample': changes[:50],
        }
