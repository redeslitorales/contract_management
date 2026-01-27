from odoo import models, fields, _


class ResPartner(models.Model):
    _inherit = 'res.partner'

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
