from odoo import models, fields, api, _
from odoo.exceptions import UserError,ValidationError
from datetime import datetime, timedelta, time
from dateutil.relativedelta import relativedelta
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
        notes = fields.Char(string="Notes")
    
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
    next_reservation_invoice_date = fields.Date(
        string='Next Reservation Invoice Date',
        copy=False,
        help='Used only while suspended. Does not replace next_invoice_date.',
    )
    last_reservation_invoice_id = fields.Many2one(
        'account.move',
        string='Last Reservation Invoice',
        copy=False,
        readonly=True,
    )
    contract_end_in_past = fields.Boolean(
        string="Contract End In Past",
        compute="_compute_contract_end_in_past",
        store=False,
    )
    customer_balance_ok = fields.Boolean(
        string="Customer Balance Nonpositive",
        compute="_compute_customer_balance_ok",
        store=False,
        help="True when the partner's total due is zero or a credit (nonpositive).",
    )
    
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

    def _compute_contract_end_in_past(self):
        today = fields.Date.context_today(self)
        for order in self:
            order.contract_end_in_past = any(
                contract.end_date and contract.end_date < today
                for contract in order.contract_ids
            )

    def _compute_customer_balance_ok(self):
        for order in self:
            partner = order.partner_id.commercial_partner_id
            total_due = partner.total_due if partner else 0.0
            order.customer_balance_ok = total_due <= 0
    
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

    def write(self, vals):
        res = super().write(vals)
        if vals.get('subscription_state') == '8_suspend':
            self.action_mark_suspended_for_reservation_billing()
        return res

    def _get_suspended_reservation_product(self):
        raw_product_id = self.env['ir.config_parameter'].sudo().get_param(
            'contract_management.suspended_reservation_product_id',
            default='0',
        )
        try:
            product_id = int(raw_product_id or 0)
        except (TypeError, ValueError):
            product_id = 0
        product = self.env['product.product'].browse(product_id).exists()
        if not product:
            raise UserError(_('Configure the Suspended Line Reservation Product in Settings first.'))
        return product

    def _get_suspended_reservation_amount(self):
        raw_amount = self.env['ir.config_parameter'].sudo().get_param(
            'contract_management.suspended_reservation_amount',
            default='7.0',
        )
        try:
            return float(raw_amount or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _advance_reservation_invoice_date(self, base_date):
        self.ensure_one()
        return base_date + relativedelta(months=1)

    def _prepare_suspended_reservation_invoice_line(self, product, amount):
        self.ensure_one()
        line_vals = {
            'product_id': product.id,
            'name': _('Line reservation fee while service is suspended'),
            'quantity': 1.0,
            'price_unit': amount,
        }

        taxes = product.taxes_id.filtered(
            lambda tax: not tax.company_id or tax.company_id == self.company_id
        )
        if taxes:
            fiscal_position = self.fiscal_position_id or self.partner_id.property_account_position_id
            taxes = fiscal_position.map_tax(taxes) if fiscal_position else taxes
            line_vals['tax_ids'] = [(6, 0, taxes.ids)]

        account = product.property_account_income_id or product.categ_id.property_account_income_categ_id
        if account:
            line_vals['account_id'] = account.id

        return line_vals

    def _create_suspended_reservation_invoice(self):
        self.ensure_one()

        if self.subscription_state != '8_suspend':
            raise ValidationError(_('Reservation invoices can only be created for suspended subscriptions.'))

        product = self._get_suspended_reservation_product()
        amount = self._get_suspended_reservation_amount()
        if amount <= 0:
            raise ValidationError(_('Suspended line reservation amount must be greater than zero.'))

        today = fields.Date.context_today(self)
        reservation_date = self.next_reservation_invoice_date or self.next_invoice_date or today
        origin = '%s - suspended reservation - %s' % (self.name, reservation_date)

        existing_invoice = self.env['account.move'].sudo().search([
            ('move_type', '=', 'out_invoice'),
            ('state', '!=', 'cancel'),
            ('invoice_origin', '=', origin),
            ('partner_id', '=', self.partner_invoice_id.id),
        ], limit=1)
        if existing_invoice:
            return existing_invoice

        invoice_vals = self._prepare_invoice()
        invoice_vals.update({
            'invoice_date': today,
            'invoice_origin': origin,
            'invoice_line_ids': [(0, 0, self._prepare_suspended_reservation_invoice_line(product, amount))],
        })

        invoice = self.env['account.move'].sudo().create(invoice_vals)
        invoice.action_post()

        self.sudo().write({
            'next_reservation_invoice_date': self._advance_reservation_invoice_date(reservation_date),
            'last_reservation_invoice_id': invoice.id,
        })

        self.message_post(body=_(
            'Suspended line reservation invoice %s created for %s. Service next invoice date remains %s. Next reservation invoice date is %s.'
        ) % (
            invoice.display_name,
            reservation_date,
            self.next_invoice_date,
            self.next_reservation_invoice_date,
        ))

        return invoice

    @api.model
    def cron_create_suspended_reservation_invoices(self):
        today = fields.Date.context_today(self)
        subscriptions = self.sudo().search([
            ('is_subscription', '=', True),
            ('state', '=', 'sale'),
            ('subscription_state', '=', '8_suspend'),
            ('next_reservation_invoice_date', '!=', False),
            ('next_reservation_invoice_date', '<=', today),
        ])
        for subscription in subscriptions:
            try:
                subscription._create_suspended_reservation_invoice()
            except Exception as exc:
                subscription.message_post(body=_(
                    'Failed to create suspended line reservation invoice: %s'
                ) % exc)
        return True

    def action_mark_suspended_for_reservation_billing(self):
        for order in self:
            if order.subscription_state != '8_suspend':
                continue
            if not order.next_reservation_invoice_date:
                order.next_reservation_invoice_date = order.next_invoice_date or fields.Date.context_today(order)

    def reactivate_service(self):
        """Reactivate a suspended subscription with ONU and state updates.
        
        If payment brought the account out of suspension, this ensures both
        ONU reactivation and subscription_state are synchronized.
        """
        self.ensure_one()
        try:
            if self.cpe_unit_asset:
                self.cpe_unit_asset.reactivate_onu()
                # Verify ONU is now enabled
                if self.cpe_unit_asset.onu_state == 'enabled':
                    self.write({
                        'subscription_state': '3_progress',
                        'internet_service_state': 'active',
                        'suspension_effective_date': False,
                        'suspension_reason': False,
                    })
                    self.message_post(body="✅ Subscription reactivated: ONU enabled and state restored to active.")
                    return True
                else:
                    self.message_post(body="⚠️ Subscription reactivation incomplete: ONU enable failed.")
                    return False
            else:
                # No CPE asset, just update state
                self.write({
                    'subscription_state': '3_progress',
                    'internet_service_state': 'active',
                    'suspension_effective_date': False,
                    'suspension_reason': False,
                })
                self.message_post(body="✅ Subscription state restored to active (no ONU asset).")
                return True
        except Exception as e:
            _logger.exception("Error during reactivate_service for %s", self.name)
            self.message_post(body=f"❌ Reactivation error: {str(e)}")
            return False

    def reactivate_subscription_now(self):
        """Bring a suspended subscription back to active service and invoice immediately."""
        self.ensure_one()
        today = fields.Date.context_today(self)

        if self.cpe_unit_asset:
            self.enable_onu()

        self.write({
            'subscription_state': '3_progress',
            'internet_service_state': 'active',
            'suspension_effective_date': False,
            'suspension_reason': False,
            'next_invoice_date': today,
            'next_reservation_invoice_date': False,
        })

        invoices = self._create_invoices()
        if invoices:
            invoices.action_post()

        # Ensure next cycle moves forward for normal service billing.
        if self.next_invoice_date and self.next_invoice_date <= today:
            self.write({'next_invoice_date': today + relativedelta(months=1)})

        self.message_post(body=_(
            'Subscription reactivated and normal service invoice created from %s.'
        ) % today)
        return True

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
            'renewal_of_id': subscription.id if subscription_state == '2_renewal' else False,
            'upsell_from_id': subscription.id if subscription_state == '7_upsell' else False,
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
        subscription.internet_service_state = 'paused'
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
    reactivation_date = fields.Date(string='Reactivation Date', default=fields.Date.context_today, required=True)

    def action_reactivate_subscription(self):
        self.ensure_one()
        subscription = self.subscription_id

        # Delete any existing scheduled actions for this subscription
        cron_jobs = self.env['ir.cron'].search([('name', 'ilike', 'Service ' + str(subscription.name))])
        if cron_jobs:
            cron_jobs.unlink()

        today = fields.Date.context_today(self)

        # If the reactivation date is today or in the past, reactivate immediately
        if self.reactivation_date <= today:
            subscription.reactivate_subscription_now()
        else:
            # Schedule the enable_onu method on the reactivation date
            self.env['ir.cron'].create({
                'name': 'Reactivate Service ' + str(subscription.name),
                'model_id': self.env.ref('sale.model_sale_order').id,
                'state': 'code',
                'code': f"model.browse({subscription.id}).sudo().reactivate_subscription_now()",
                'nextcall': self.reactivation_date,
                'numbercall': 1,
            })

        # Log the activity
        subscription.message_post(body=f"Subscription reactivated by user on {self.reactivation_date}.")

        return {'type': 'ir.actions.act_window_close'}