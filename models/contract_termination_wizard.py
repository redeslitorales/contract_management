from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.tools import float_compare


class ContractTerminationWizard(models.TransientModel):
    _name = 'contract.termination.wizard'
    _description = 'Contract Termination Wizard'

    STATE_COST = 'cost'
    STATE_APPROVAL = 'approval'
    STATE_EQUIPMENT = 'equipment'
    STATE_CLOSURE = 'closure'

    state = fields.Selection([
        (STATE_COST, 'Early Termination Cost'),
        (STATE_APPROVAL, 'Manager Approval'),
        (STATE_EQUIPMENT, 'Equipment Return'),
        (STATE_CLOSURE, 'Information Collection'),
    ], string='Step', default=STATE_COST)

    contract_id = fields.Many2one('contract.management', string='Contract', required=True)
    subscription_id = fields.Many2one(related='contract_id.subscription_id', string='Subscription', readonly=True)
    partner_id = fields.Many2one(related='contract_id.partner_id', string='Partner', readonly=True, store=False)
    early_termination_cost = fields.Float(
        string='Early Termination Cost',
        readonly=True,
        compute='_compute_early_termination_cost',
        help="Early termination cost displayed as a positive amount for customer clarity.")
    requires_manager_approval = fields.Boolean(string='Requires Manager Approval', compute='_compute_requires_manager_approval')
    payment_id = fields.Many2one(
        'account.payment',
        string='Payment',
        required=True,
        help="Select the payment that settles the early termination cost.")

    payment_confirmed = fields.Boolean(string='Client paid early termination cost', help="Confirm the client has paid the early termination cost shown above.")
    equipment_returned = fields.Boolean(string='All equipment returned', help="Confirm the client returned all issued equipment.")
    manager_approved = fields.Boolean(string='Manager approved termination', readonly=True)
    manager_user_id = fields.Many2one('res.users', string='Approved by', readonly=True)

    reason = fields.Many2one('sale.order.close.reason', string='Reason for Closure')
    notes = fields.Char(string='Notes')
    other_reason = fields.Char(string='Reasons for Other')
    accepted_better_offer = fields.Boolean(string='Accepted a Better Offer?')
    carrier = fields.Many2one('subscription.competitor', string='Competitor')
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
    ], string='Rate Our Service')
    problems_experienced = fields.Many2many('subscription.problem', string='Problems Experienced')

    @api.model
    def default_get(self, fields_list):
        defaults = super().default_get(fields_list)
        contract_id = self.env.context.get('default_contract_id') or self.env.context.get('active_id')
        if contract_id:
            defaults['contract_id'] = contract_id
        return defaults

    def _reload_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'view_mode': 'form',
            'res_id': self.id,
            'target': 'new',
            'context': self.env.context,
        }

    @api.depends('contract_id')
    def _compute_early_termination_cost(self):
        for wizard in self:
            cost = wizard.contract_id.early_termination_cost or 0.0
            wizard.early_termination_cost = abs(cost)

    @api.depends('early_termination_cost')
    def _compute_requires_manager_approval(self):
        for wizard in self:
            wizard.requires_manager_approval = float_compare(
                wizard.early_termination_cost or 0.0,
                100.0,
                precision_digits=2,
            ) == -1

    @api.constrains('payment_id', 'early_termination_cost')
    def _check_payment_amount(self):
        """Validate that the payment amount matches the early termination cost."""
        for wizard in self:
            if wizard.payment_id and wizard.early_termination_cost:
                payment_amount = abs(wizard.payment_id.amount)
                cost = wizard.early_termination_cost
                # Use float_compare to handle floating point precision issues
                if float_compare(payment_amount, cost, precision_digits=2) != 0:
                    raise ValidationError(_(
                        'The payment amount ($%.2f) does not match the early termination cost ($%.2f). '
                        'Please select a payment with the exact amount.'
                    ) % (payment_amount, cost))

    def action_next_step(self):
        self.ensure_one()
        if self.state == self.STATE_COST:
            if not self.payment_confirmed:
                raise ValidationError(_('Please check "Client paid early termination cost" before continuing.'))
            self.state = self.STATE_APPROVAL if self.requires_manager_approval else self.STATE_EQUIPMENT
        elif self.state == self.STATE_APPROVAL:
            if self.requires_manager_approval and not self.manager_approved:
                raise ValidationError(_('A contract manager must approve because the early termination cost is under $100.'))
            self.state = self.STATE_EQUIPMENT
        elif self.state == self.STATE_EQUIPMENT:
            self.state = self.STATE_CLOSURE
        return self._reload_wizard()

    def action_back_to_cost(self):
        self.ensure_one()
        if self.state == self.STATE_CLOSURE:
            self.state = self.STATE_EQUIPMENT
        elif self.state == self.STATE_EQUIPMENT:
            self.state = self.STATE_APPROVAL if self.requires_manager_approval else self.STATE_COST
        elif self.state == self.STATE_APPROVAL:
            self.state = self.STATE_COST
        return self._reload_wizard()

    def action_manager_approve(self):
        self.ensure_one()
        if not (self.env.user.has_group('contract_management.group_contract_management_manager') or
                self.env.user.has_group('base.group_system')):
            raise ValidationError(_('Only a contract manager can approve this termination. If you are not a manager, please ask one to continue.'))
        self.manager_approved = True
        self.manager_user_id = self.env.user
        # Move forward automatically once approved
        self.state = self.STATE_EQUIPMENT
        return self._reload_wizard()

    @api.onchange('contract_id', 'early_termination_cost')
    def _onchange_payment_domain(self):
        partner_id = self.contract_id.partner_id.id if self.contract_id else False
        # Filter by partner only to avoid float rounding issues; amount is validated server-side in action_confirm_termination
        return {'domain': {'payment_id': [('partner_id', '=', partner_id)]}}

    @api.onchange('accepted_better_offer')
    def _onchange_accepted_better_offer(self):
        if not self.accepted_better_offer:
            self.carrier = False
            self.bandwidth = False
            self.upload = False
            self.tv_included = False
            self.telephone_included = False
            self.monthly_payment = False

    def action_confirm_termination(self):
        self.ensure_one()
        if not self.payment_confirmed:
            raise ValidationError(_('Please confirm the client has paid the early termination cost to proceed.'))
        if not self.equipment_returned:
            raise ValidationError(_('Please confirm the client returned all issued equipment before closing the contract.'))
        if self.requires_manager_approval and not self.manager_approved:
            raise ValidationError(_('Manager approval is required because the early termination cost is under $100.'))

        if not self.reason:
            raise ValidationError(_('Please select a reason for closure.'))
        if not self.notes:
            raise ValidationError(_('Please add notes about this termination.'))
        if not self.service_rating:
            raise ValidationError(_('Please rate our service before finishing the termination.'))

        payment = self.payment_id
        if not payment:
            raise ValidationError(_('Select the payment that covers the early termination cost shown above.'))

        rounding = payment.currency_id.rounding if payment.currency_id else self.env.company.currency_id.rounding
        if float_compare(payment.amount or 0.0, self.early_termination_cost or 0.0, precision_rounding=rounding) != 0:
            raise ValidationError(_(
                'The selected payment amount does not match the early termination cost (expected %(expected).2f, got %(actual).2f). Please choose a payment for the exact amount.'
            ) % {
                'expected': self.early_termination_cost,
                'actual': payment.amount or 0.0,
            })
        if payment.partner_id != self.contract_id.partner_id:
            raise ValidationError(_(
                'The selected payment belongs to %(payment_partner)s. Please choose a payment from %(contract_partner)s.'
            ) % {
                'payment_partner': payment.partner_id.display_name,
                'contract_partner': self.contract_id.partner_id.display_name,
            })

        self.contract_id._terminate_with_checks(
            payment_confirmed=True,
            equipment_returned=True,
            via_wizard=True,
            payment=payment,
        )

        subscription = self.contract_id.subscription_id or self.subscription_id
        if subscription:
            closure_vals = {
                'subscription_id': subscription.id,
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
            }
            self.env['subscription.closure'].create(closure_vals)
            subscription.write({
                'subscription_state': '6_churn',
                'close_reason_id': self.reason.id,
                'end_date': fields.Datetime.now(),
            })
            if self.reason.early_termination:
                subscription.write({'state': 'cancel'})

        return {'type': 'ir.actions.act_window_close'}