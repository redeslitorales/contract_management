from odoo import models, fields, api, _
from odoo.exceptions import UserError,ValidationError
from datetime import datetime, timedelta, time
import pytz
import requests, json, time, unicodedata
import logging
_logger = logging.getLogger(__name__)

class SubscriptionProblem(models.Model):
    _name = 'subscription.problem'
    _description = 'Subscription Problem Model'

    name = fields.Char(string='Problem', required=True)
    
class SubcriptionCompetitor(models.Model):
        _name = 'subscription.competitor'
        _description = 'Subscription Competitor Model'
        
        name = fields.Char(string="Name", compute= '_compute_name')
        competitor = fields.Char(string='Competitor', required=True)    

        @api.depends('competitor')
        def _compute_name(self):
            for rec in self:
                rec.name = rec.competitor

    
class SubscriptionClosure(models.Model):
        _name = 'subscription.closure'
        _description = 'Subscription Closure Model'
        
        name = fields.Char(string="Name", compute="_compute_name")
        
        partner_id = fields.Many2one(related='subscription_id.partner_id', string="Client")
        subscription_id = fields.Many2one('sale.order', string="Subscription")
        date = fields.Datetime('Effective Date', default=datetime.now())
        reason = fields.Many2one('sale.order.close.reason', string='Reason for Cancelation')
        accepted_better_offer = fields.Boolean(string='Accepted a Better Offer?')
        carrier = fields.Many2one('subscription.competitor', string="Competitor")
        bandwidth = fields.Char(string='Download Speed (Mbps)')
        upload = fields.Char(string='Upload Speed (Mbps)')
        tv_included = fields.Boolean(string='Cable TV')
        telephone_included = fields.Boolean(string='Telephone')
        monthly_payment = fields.Float(string='Monthly Quota')
        service_rating = fields.Selection([
                ('1', '1 - Very Poor'),
                ('2', '2 - Poor'),
                ('3', '3 - Average'),
                ('4', '4 - Good'),
                ('5', '5 - Excellent')
            ], string='Rate Our Service', required=True)
        notes = fields.Char(string="Notes", required=True)
    
        problems_experienced = fields.Many2many('subscription.problem', string='Problems Experienced')
        closure_date = fields.Datetime(string='Closure Date', default=fields.Datetime.now)      
        other_reason = fields.Char(string="Reasons for Other")  
        
        @api.depends('partner_id', 'subscription_id')
        def _compute_name(self):
            for record in self:
                record.name = str(record.partner_id.name)+' - '+str(record.subscription_id.name)

class SubscriptionClosureWizard(models.TransientModel):
    _name = 'subscription.closure.wizard'
    _description = 'Subscription Closure Wizard'

    subscription_id = fields.Many2one('sale.order', string="Subscription")
    reason = fields.Many2one('sale.order.close.reason', string='Reason for Closure', required=True)
    notes = fields.Char(string="Notes", required=True)
    other_reason = fields.Char(string="Reasons for Other")  

    accepted_better_offer = fields.Boolean(string='Accepted a Better Offer?')
    
    carrier = fields.Many2one('subscription.competitor', string="Competitor")
    bandwidth = fields.Integer(string='Download Speed')
    upload = fields.Integer(string='Upload Speed')
    tv_included = fields.Boolean(string='TV Included?')
    telephone_included = fields.Boolean(string='Telephone Included?')
    monthly_payment = fields.Float(string='Monthly Payment')
    
    service_rating = fields.Selection([
        ('1', '1 - Very Poor'),
        ('2', '2 - Poor'),
        ('3', '3 - Average'),
        ('4', '4 - Good'),
        ('5', '5 - Excellent')
    ], string='Rate Our Service', required=True)
    
    problems_experienced = fields.Many2many('subscription.problem', string='Problems Experienced')

    @api.model
    def default_get(self, fields):
        res = super(SubscriptionClosureWizard, self).default_get(fields)
        if self.env.context.get('subscription_id'):
            res['subscription_id'] = self.env.context['subscription_id']
        return res

    @api.onchange('accepted_better_offer')
    def _onchange_accepted_better_offer(self):
        if not self.accepted_better_offer:
            self.carrier = False
            self.bandwidth = False
            self.tv_included = False
            self.telephone_included = False
            self.monthly_payment = False
            
    def action_confirm(self):
        self.env['subscription.closure'].create({
            'subscription_id': self.subscription_id.id,
            'reason': self.reason.id,
            'notes': self.notes,
            'other_reason': self.other_reason,
            'accepted_better_offer': self.accepted_better_offer,
            'carrier': self.carrier.id,
            'bandwidth': self.bandwidth,
            'upload': self.upload,
            'tv_included': self.tv_included,
            'telephone_included': self.telephone_included,
            'monthly_payment': self.monthly_payment,
            'service_rating': self.service_rating,
            'problems_experienced': [(6, 0, self.problems_experienced.ids)],
            'closure_date': fields.Datetime.now(),
        })
        # Close subscription before canceling to avoid error
        self.subscription_id.write({'subscription_state': '6_churn','close_reason_id': self.reason.id, 'end_date': fields.Datetime.now()})
        if self.reason.early_termination:
            self.subscription_id.write({'state': 'cancel'})
        return {'type': 'ir.actions.act_window_close'}

class SubscriptionClose(models.Model):
    _inherit = 'sale.order'
    
    sub_pause_start_date = fields.Datetime(string="Subscription Pause Date")
    sub_pause_end_date = fields.Datetime(string="Anticipated Reactivation Date")
    
    def action_open_closure_wizard(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Subscription Closure Wizard',
            'res_model': 'subscription.closure.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_subscription_id': self.id,
            },
        }
    
    def action_pause_subscription_wizard(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Pause Subscription Wizard',
            'res_model': 'pause.subscription.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_subscription_id': self.id,
            },
        }
    
    def action_reactivate_subscription_wizard(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Reactivate Subscription Wizard',
            'res_model': 'reactivate.subscription.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_subscription_id': self.id,
            },
        }

#  Redefining methods from the sale_subscription.sale_order.py file to accommadate CPE 

    def _get_order_digest(self, origin='', template='sale_subscription.sale_order_digest', lang=None):
        self.ensure_one()
        values = {'origin': origin,
                  'record_url': self._get_html_link(),
                  'start_date': self.start_date,
                  'next_invoice_date': self.next_invoice_date,
                  'recurring_monthly': self.recurring_monthly,
                  'untaxed_amount': self.amount_untaxed,
                  'cpe_unit':self.cpe_unit,
                  'cpe_unit_asset':self.cpe_unit_asset,
                  'quotation_template': self.sale_order_template_id.name} # see if we don't want plan instead
        return self.env['ir.qweb'].with_context(lang=lang)._render(template, values)
    
    def _prepare_upsell_renew_order_values(self, subscription_state):
        """
        Create a new draft order with the same lines as the parent subscription. All recurring lines are linked to their parent lines
        :return: dict of new sale order values
        """
        self.ensure_one()
        today = fields.Date.today()
        if subscription_state == '7_upsell' and self.next_invoice_date <= max(self.first_contract_date or today, today):
            raise UserError(_('You cannot create an upsell for this subscription because it :\n'
                              ' - Has not started yet.\n'
                              ' - Has no invoiced period in the future.'))
        subscription = self.with_company(self.company_id)
        order_lines = self.order_line._get_renew_upsell_values(subscription_state, period_end=self.next_invoice_date)
        is_subscription = subscription_state in ['2_renewal', '7_upsell']
        option_lines_data = [Command.link(option.copy().id) for option in subscription.sale_order_option_ids]
        if subscription_state == '7_upsell':
            start_date = fields.Date.today()
            next_invoice_date = self.next_invoice_date
        else:
            # renewal
            start_date = self.next_invoice_date
            next_invoice_date = self.next_invoice_date # the next invoice date is the start_date for new contract
        return {
            'is_subscription': is_subscription,
            'subscription_id': subscription.id,
            'pricelist_id': subscription.pricelist_id.id,
            'partner_id': subscription.partner_id.id,
            'partner_invoice_id': subscription.partner_invoice_id.id,
            'partner_shipping_id': subscription.partner_shipping_id.id,
            'order_line': order_lines,
            'analytic_account_id': subscription.analytic_account_id.id,
            'subscription_state': subscription_state,
            'origin': subscription.client_order_ref,
            'client_order_ref': subscription.client_order_ref,
            'origin_order_id': subscription.id,
            'note': subscription.note,
            'user_id': subscription.user_id.id,
            'payment_term_id': subscription.payment_term_id.id,
            'company_id': subscription.company_id.id,
            'sale_order_template_id': self.sale_order_template_id.id,
            'sale_order_option_ids': option_lines_data,
            'payment_token_id': False,
            'start_date': start_date,
            'next_invoice_date': next_invoice_date,
            'plan_id': subscription.plan_id.id,
            'cpe_unit': subscription.cpe_unit.id,
            'cpe_unit_asset': subscription.cpe_unit_asset.id,
        }
    
class SubscriptionCloseReasonCustom(models.Model):
    _inherit = 'sale.order.close.reason'
    
    early_termination = fields.Boolean("Early Termination")
    
####  Pause Subscription

class PauseSubscriptionWizard(models.TransientModel):
    _name = 'pause.subscription.wizard'
    _description = 'Wizard to Pause Subscription'

    subscription_id = fields.Many2one('sale.order', string='Subscription', required=True)
    pause_start_date = fields.Datetime(string='Pause Start Date', required=True)
    pause_end_date = fields.Datetime(string='Pause End Date')
    
    def action_pause_subscription(self):
        self.ensure_one()
        subscription = self.subscription_id

       # Calculate the pause duration
        if self.pause_end_date:
            pause_duration = (self.pause_end_date - self.pause_start_date).days
        else:
            pause_duration = 90

        # Pause the subscription
        subscription.subscription_state = '4_paused'
        subscription.sub_pause_start_date = self.pause_start_date
        subscription.sub_pause_end_date = self.pause_end_date
        if self.pause_end_date:
            subscription.next_invoice_date = self.pause_end_date
        else:
            subscription.next_invoice_date = self.pause_start_date + timedelta(days=pause_duration)

        # Log the activity
        subscription.message_post(body=f"Subscription paused by user from {self.pause_start_date} to {subscription.next_invoice_date}.")

        # Send notification email
#        template = self.env.ref('subscription.pause_notification_template')
#        self.env['mail.template'].browse(template.id).send_mail(subscription.id)

        # Schedule the disable_onu method on the pause start date
        if subscription.cpe_unit_asset:
            self.env['ir.cron'].create({
                'name': 'Pause Service '+str(subscription.name),
                'model_id': self.env.ref('sale.model_sale_order').id,
                'state': 'code',
                'code': f'model.browse({subscription.id}).disable_onu()',
                'nextcall': self.pause_start_date,
                'numbercall': 1,
            })

        # Schedule the enable_onu method on the pause start date
        if subscription.cpe_unit_asset:
            self.env['ir.cron'].create({
                'name': 'Reactivate Service '+str(subscription.name),
                'model_id': self.env.ref('sale.model_sale_order').id,
                'state': 'code',
                'code': f'model.browse({subscription.id}).enable_onu()',
                'nextcall': subscription.next_invoice_date,
                'numbercall': 1,
            })
        return {'type': 'ir.actions.act_window_close'}

        # Schedule the invoice creation on the reactivation date
        self.env['ir.cron'].create({
            'name': 'Create Invoice for Service ' + str(subscription.name),
            'model_id': self.env.ref('sale.model_sale_order').id,
            'state': 'code',
            'code': f'model.browse({subscription.id})._create_invoice()',
            'nextcall': subscription.next_invoice_date,
            'numbercall': 1,
        })


class ReactivateSubscriptionWizard(models.TransientModel):
    _name = 'reactivate.subscription.wizard'
    _description = 'Wizard to Reactivate Subscription'

    subscription_id = fields.Many2one('sale.order', string='Subscription', required=True)
    reactivation_date = fields.Datetime(string='Reactivation Date', default=fields.Date.context_today, required=True)

    def action_reactivate_subscription(self):
        self.ensure_one()
        subscription = self.subscription_id

        # Get the current date in the user's timezone
        user_tz = self.env.user.tz or 'CST'
        timezone = pytz.timezone(user_tz)
        current_date = datetime.now(timezone).date()

        # Delete any existing scheduled actions for this subscription
        cron_jobs = self.env['ir.cron'].search([('name', 'ilike', 'Service ' + str(subscription.name))])
        if cron_jobs:
            cron_jobs.unlink()

        # If the reactivation date is today, enable ONU, set subscription state, and create invoice
        if self.reactivation_date == current_date:
            if subscription.cpe_unit_asset:
                activated = subscription.enable_onu()
                if activated:
                    subscription.subscription_state = '	3_progress'
                    invoice = subscription._create_invoices()
                    posted = invoice.action_post()
        else:
            # Schedule the enable_onu method on the reactivation date
            self.env['ir.cron'].create({
                'name': 'Reactivate Service ' + str(subscription.name),
                'model_id': self.env.ref('sale.model_sale_order').id,
                'state': 'code',
                'code': f'model.browse({subscription.id}).enable_onu()',
                'nextcall': self.reactivation_date,
                'numbercall': 1,
            })

            # Schedule the invoice creation on the reactivation date
            self.env['ir.cron'].create({
                'name': 'Create Invoice for ' + str(subscription.name),
                'model_id': self.env.ref('sale.model_sale_order').id,
                'state': 'code',
                'code': f'model.browse({subscription.id})._create_invoice()',
                'nextcall': self.reactivation_date,
                'numbercall': 1,
            })

        # Log the activity
        subscription.message_post(body=f"Subscription reactivated by user on {self.reactivation_date}.")

        return {'type': 'ir.actions.act_window_close'}