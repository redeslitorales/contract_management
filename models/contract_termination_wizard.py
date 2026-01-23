from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.tools import float_compare


class ContractTerminationWizard(models.TransientModel):
    _name = 'contract.termination.wizard'
    _description = 'Contract Termination Wizard'

    STATE_COST = 'cost'
    STATE_APPROVAL = 'approval'
    STATE_PAYMENT = 'payment'
    STATE_EQUIPMENT = 'equipment'
    STATE_CLOSURE = 'closure'

    state = fields.Selection([
        (STATE_COST, 'Early Termination Cost'),
        (STATE_APPROVAL, 'Manager Approval'),
        (STATE_PAYMENT, 'Payment Confirmation'),
        (STATE_EQUIPMENT, 'Equipment Return'),
        (STATE_CLOSURE, 'Information Collection'),
    ], string='Step', default=STATE_COST)

    contract_id = fields.Many2one('contract.management', string='Contract', required=True)
    subscription_id = fields.Many2one(related='contract_id.subscription_id', string='Subscription', readonly=True)
    partner_id = fields.Many2one(related='contract_id.partner_id', string='Partner', readonly=True, store=False)
    request_id = fields.Many2one('contract.termination.request', string='Termination Request', readonly=True)
    early_termination_cost = fields.Float(
        string='Early Termination Cost',
        readonly=True,
        compute='_compute_early_termination_cost',
        help="Early termination cost displayed as a positive amount for customer clarity.")
    applied_termination_cost = fields.Float(
        string='Applied Termination Cost',
        digits=(16, 2),
        help="Termination cost to enforce; defaults to the computed early termination cost and can be overridden by a supervisor.")
    cost_override_requested = fields.Boolean(string='Override Requested', readonly=True)
    cost_override_request_reason = fields.Text(string='Override Request Justification')
    cost_override_request_user_id = fields.Many2one('res.users', string='Override Requested By', readonly=True)
    cost_override_request_datetime = fields.Datetime(string='Override Requested On', readonly=True)
    cost_override_attachment_ids = fields.Many2many(
        'ir.attachment',
        'contract_term_wizard_override_ir_attachment_rel',
        'wizard_id',
        'attachment_id',
        string='Supporting Documents')
    cost_override_reason = fields.Text(string='Override Reason')
    cost_override_applied = fields.Boolean(string='Override Applied', readonly=True)
    cost_override_user_id = fields.Many2one('res.users', string='Override Approved By', readonly=True)
    cost_override_datetime = fields.Datetime(string='Override Approved On', readonly=True)
    can_override_cost = fields.Boolean(string='Can Override Cost', compute='_compute_can_override_cost')
    requires_manager_approval = fields.Boolean(string='Requires Manager Approval', compute='_compute_requires_manager_approval')
    payment_id = fields.Many2one(
        'account.payment',
        string='Payment',
        required=False,
        help="Select the payment that settles the early termination cost.")

    payment_confirmed = fields.Boolean(string='Client paid early termination cost', help="Confirm the client has paid the early termination cost shown above.")
    customer_requests_waiver = fields.Boolean(string='Customer requests waiver/reduction', help="Check if the customer is asking to reduce or waive the termination fee.")
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
            contract = self.env['contract.management'].browse(contract_id)
            request = self._get_or_create_request(contract)
            defaults['request_id'] = request.id

            defaults.setdefault('applied_termination_cost', request.applied_termination_cost or abs(contract.early_termination_cost or 0.0))
            defaults.setdefault('cost_override_requested', request.cost_override_requested)
            defaults.setdefault('cost_override_request_reason', request.cost_override_request_reason)
            defaults.setdefault('cost_override_request_user_id', request.cost_override_request_user_id.id)
            defaults.setdefault('cost_override_request_datetime', request.cost_override_request_datetime)
            defaults.setdefault('cost_override_reason', request.cost_override_reason)
            defaults.setdefault('cost_override_applied', request.cost_override_applied)
            defaults.setdefault('cost_override_user_id', request.cost_override_user_id.id)
            defaults.setdefault('cost_override_datetime', request.cost_override_datetime)
            defaults.setdefault('payment_id', request.payment_id.id)
            defaults.setdefault('payment_confirmed', request.payment_confirmed)
            defaults.setdefault('customer_requests_waiver', request.customer_requests_waiver)
            defaults.setdefault('equipment_returned', request.equipment_returned)
            defaults.setdefault('manager_approved', request.manager_approved)
            defaults.setdefault('manager_user_id', request.manager_user_id.id)
            defaults.setdefault('state', request.wizard_state or defaults.get('state'))
            defaults.setdefault('reason', request.reason.id)
            defaults.setdefault('notes', request.notes)
            defaults.setdefault('other_reason', request.other_reason)
            defaults.setdefault('accepted_better_offer', request.accepted_better_offer)
            defaults.setdefault('carrier', request.carrier.id)
            defaults.setdefault('bandwidth', request.bandwidth)
            defaults.setdefault('upload', request.upload)
            defaults.setdefault('tv_included', request.tv_included)
            defaults.setdefault('telephone_included', request.telephone_included)
            defaults.setdefault('monthly_payment', request.monthly_payment)
            defaults.setdefault('service_rating', request.service_rating)
            defaults.setdefault('cost_override_attachment_ids', [(6, 0, request.cost_override_attachment_ids.ids)])
            defaults.setdefault('problems_experienced', [(6, 0, request.problems_experienced.ids)])
        return defaults

    def _get_or_create_request(self, contract):
        request = self.env['contract.termination.request'].search([
            ('contract_id', '=', contract.id),
            ('state', '=', 'open'),
        ], limit=1)
        if request:
            return request
        applied_cost = abs(contract.early_termination_cost or 0.0)
        return self.env['contract.termination.request'].create({
            'contract_id': contract.id,
            'applied_termination_cost': applied_cost,
            'wizard_state': self.STATE_COST,
        })

    def _save_to_request(self):
        self.ensure_one()
        contract = self.contract_id
        if not contract:
            return
        request = self.request_id or self._get_or_create_request(contract)
        self.request_id = request

        write_vals = {
            'applied_termination_cost': self.applied_termination_cost,
            'cost_override_requested': self.cost_override_requested,
            'cost_override_request_reason': self.cost_override_request_reason,
            'cost_override_request_user_id': self.cost_override_request_user_id.id,
            'cost_override_request_datetime': self.cost_override_request_datetime,
            'cost_override_reason': self.cost_override_reason,
            'cost_override_applied': self.cost_override_applied,
            'cost_override_user_id': self.cost_override_user_id.id,
            'cost_override_datetime': self.cost_override_datetime,
            'payment_id': self.payment_id.id,
            'payment_confirmed': self.payment_confirmed,
            'customer_requests_waiver': self.customer_requests_waiver,
            'equipment_returned': self.equipment_returned,
            'manager_approved': self.manager_approved,
            'manager_user_id': self.manager_user_id.id,
            'wizard_state': self.state,
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
            'cost_override_attachment_ids': [(6, 0, self.cost_override_attachment_ids.ids)],
        }
        request.write(write_vals)

    def _can_current_user_override_cost(self):
        return (
            self.env.user.has_group('contract_management.group_contract_management_manager') or
            self.env.user.has_group('base.group_system')
        )

    @api.depends('early_termination_cost')
    def _compute_can_override_cost(self):
        for wizard in self:
            wizard.can_override_cost = wizard._can_current_user_override_cost()

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

    @api.depends('applied_termination_cost', 'early_termination_cost', 'cost_override_requested')
    def _compute_requires_manager_approval(self):
        for wizard in self:
            if wizard.cost_override_requested:
                wizard.requires_manager_approval = True
                continue
            applied_cost = wizard.applied_termination_cost if wizard.applied_termination_cost is not None else wizard.early_termination_cost
            wizard.requires_manager_approval = float_compare(
                applied_cost or 0.0,
                100.0,
                precision_digits=2,
            ) == -1

    def _requires_payment_step(self, applied_cost):
        return (
            self.cost_override_applied
            and self.manager_approved
            and float_compare(applied_cost or 0.0, 0.0, precision_digits=2) == 1
        )

    def _ensure_manager_is_not_requester(self):
        self.ensure_one()
        request = self.request_id or self._get_or_create_request(self.contract_id)
        requester = request.create_uid
        override_requester = request.cost_override_request_user_id
        if requester and requester.id == self.env.user.id:
            raise ValidationError(_('The manager approving the termination must differ from the user who initiated the request.'))
        if override_requester and override_requester.id == self.env.user.id:
            raise ValidationError(_('The manager approving the waiver or reduction must differ from the user who requested it.'))

    def _meets_next_conditions(self, applied_cost):
        """Evaluate whether the wizard can advance past cost/approval.

        Allowed paths:
        1) Standard flow: termination cost is paid; if the cost is below $100, a manager must approve.
        2) Waiver: manager approved the waiver/reduction and the applied cost is $0.
        3) Reduction: manager approved the reduction and the reduced cost has been paid.
        """
        cost_positive = float_compare(applied_cost or 0.0, 0.0, precision_digits=2) == 1
        cost_zero = float_compare(applied_cost or 0.0, 0.0, precision_digits=2) == 0
        cost_under_100 = float_compare(applied_cost or 0.0, 100.0, precision_digits=2) == -1

        payment_done = bool(self.payment_confirmed) if cost_positive else False
        manager_ok = bool(self.manager_approved)

        standard_paid = cost_positive and payment_done and (not cost_under_100 or manager_ok)
        waiver_approved = cost_zero and self.cost_override_applied and manager_ok
        reduction_paid = cost_positive and self.cost_override_applied and manager_ok and payment_done

        return standard_paid or waiver_approved or reduction_paid

    @api.constrains('applied_termination_cost')
    def _check_applied_cost(self):
        for wizard in self:
            applied_cost = wizard.applied_termination_cost if wizard.applied_termination_cost is not None else wizard.early_termination_cost or 0.0
            if float_compare(applied_cost or 0.0, 0.0, precision_digits=2) < 0:
                raise ValidationError(_('Applied termination cost cannot be negative.'))
            if float_compare(applied_cost or 0.0, wizard.early_termination_cost or 0.0, precision_digits=2) != 0 and not wizard._can_current_user_override_cost():
                raise ValidationError(_('Only a contract manager or administrator can override the termination cost.'))

    @api.constrains('payment_id', 'early_termination_cost')
    def _check_payment_amount(self):
        """Validate that the payment amount matches the early termination cost."""
        for wizard in self:
            applied_cost = wizard.applied_termination_cost if wizard.applied_termination_cost is not None else wizard.early_termination_cost or 0.0
            if wizard.payment_id and float_compare(applied_cost or 0.0, 0.0, precision_digits=2) == 1:
                payment_amount = abs(wizard.payment_id.amount)
                cost = abs(applied_cost)
                # Use float_compare to handle floating point precision issues
                if float_compare(payment_amount, cost, precision_digits=2) != 0:
                    raise ValidationError(_(
                        'The payment amount ($%.2f) does not match the termination cost to apply ($%.2f). '
                        'Please select a payment with the exact amount.'
                    ) % (payment_amount, cost))
                matched_lines = wizard.payment_id.move_id.line_ids.filtered(lambda line: line.matched_debit_ids or line.matched_credit_ids)
                if matched_lines:
                    raise ValidationError(_(
                        'The selected payment has already been applied to an invoice. '
                        'Choose an unapplied payment that matches the termination cost.'
                    ))

    def _validate_payment_for_cost(self, applied_cost):
        self.ensure_one()
        if float_compare(applied_cost or 0.0, 0.0, precision_digits=2) <= 0:
            return
        if not self.payment_confirmed:
            raise ValidationError(_('Confirm the termination cost payment before continuing.'))
        if not self.payment_id:
            raise ValidationError(_('Select a payment that matches the termination cost.'))
        payment_amount = abs(self.payment_id.amount)
        cost = abs(applied_cost)
        if float_compare(payment_amount, cost, precision_digits=2) != 0:
            raise ValidationError(_(
                'The payment amount ($%.2f) does not match the termination cost to apply ($%.2f). '
                'Please select a payment with the exact amount.'
            ) % (payment_amount, cost))
        matched_lines = self.payment_id.move_id.line_ids.filtered(lambda line: line.matched_debit_ids or line.matched_credit_ids)
        if matched_lines:
            raise ValidationError(_(
                'The selected payment has already been applied to an invoice. '
                'Choose an unapplied payment that matches the termination cost.'
            ))

    def action_request_cost_override(self):
        self.ensure_one()
        if self.cost_override_applied:
            raise ValidationError(_('An override has already been approved.'))
        if not self.customer_requests_waiver:
            raise ValidationError(_('Check the waiver/reduction request box before submitting a justification.'))
        if not self.cost_override_request_reason:
            raise ValidationError(_('Add a justification before requesting an override or waiver.'))

        self.customer_requests_waiver = True
        self.cost_override_requested = True
        self.cost_override_request_user_id = self.env.user
        self.cost_override_request_datetime = fields.Datetime.now()
        self.manager_approved = False
        self.manager_user_id = False
        self.cost_override_applied = False
        self.cost_override_user_id = False
        self.cost_override_datetime = False
        self.cost_override_reason = False

        self.contract_id.message_post(body=_(
            'Override or waiver requested by %(user)s. Justification: %(reason)s'
        ) % {
            'user': self.env.user.display_name,
            'reason': self.cost_override_request_reason,
        })

        self.state = self.STATE_APPROVAL
        self._save_to_request()
        return self._reload_wizard()

    def action_apply_cost_override(self):
        self.ensure_one()
        if not self._can_current_user_override_cost():
            raise ValidationError(_('Only a contract manager can override or waive the termination cost.'))
        if not self.cost_override_requested:
            raise ValidationError(_('Submit an override request with justification before approval.'))

        self._ensure_manager_is_not_requester()

        applied_cost = self.applied_termination_cost if self.applied_termination_cost is not None else 0.0
        original_cost = self.early_termination_cost or 0.0

        if float_compare(applied_cost or 0.0, 0.0, precision_digits=2) < 0:
            raise ValidationError(_('Applied termination cost cannot be negative.'))

        if not self.cost_override_reason:
            raise ValidationError(_('Add approval notes before applying a modified or waived termination cost.'))

        self.cost_override_applied = True
        self.cost_override_user_id = self.env.user
        self.cost_override_datetime = fields.Datetime.now()
        self.manager_approved = True
        self.manager_user_id = self.env.user

        if float_compare(applied_cost or 0.0, original_cost or 0.0, precision_digits=2) == 0:
            self.contract_id.message_post(body=_(
                'Override request reviewed by %(user)s. Termination cost remains $%(amount).2f. Notes: %(reason)s'
            ) % {
                'amount': applied_cost,
                'user': self.env.user.display_name,
                'reason': self.cost_override_reason,
            })
        else:
            self.contract_id.message_post(body=_(
                'Termination cost overridden from $%(original).2f to $%(new).2f by %(user)s. Reason: %(reason)s'
            ) % {
                'original': original_cost,
                'new': applied_cost,
                'user': self.env.user.display_name,
                'reason': self.cost_override_reason,
            })

        # If the override sets the cost to zero, no payment should be enforced
        if float_compare(applied_cost or 0.0, 0.0, precision_digits=2) == 0:
            self.payment_id = False
            self.payment_confirmed = False

        self._save_to_request()
        return self._reload_wizard()

    def action_next_step(self):
        self.ensure_one()
        applied_cost = self.applied_termination_cost if self.applied_termination_cost is not None else self.early_termination_cost or 0.0
        cost_changed = float_compare(applied_cost or 0.0, self.early_termination_cost or 0.0, precision_digits=2) != 0
        if cost_changed and not self.cost_override_applied:
            raise ValidationError(_('Apply the cost override and capture a reason before continuing.'))

        if self.state in (self.STATE_COST, self.STATE_APPROVAL):
            if self.cost_override_requested and not self.cost_override_applied:
                if self.state == self.STATE_COST:
                    self.state = self.STATE_APPROVAL
                    self._save_to_request()
                    return self._reload_wizard()
                raise ValidationError(_('A supervisor must apply the override request before continuing.'))

            cost_positive = float_compare(applied_cost or 0.0, 0.0, precision_digits=2) == 1
            if self.state == self.STATE_COST and cost_positive and not (self.payment_confirmed or self.cost_override_requested):
                raise ValidationError(_('To continue, confirm the early termination fee is paid or submit a waiver/reduction request.'))
            if self.state == self.STATE_COST and cost_positive and not self.payment_confirmed and not self.cost_override_requested:
                raise ValidationError(_('Please check "Client paid early termination cost" before continuing.'))

            if self.state == self.STATE_COST and self.requires_manager_approval and not self.manager_approved:
                self.state = self.STATE_APPROVAL
                self._save_to_request()
                return self._reload_wizard()

            if self.state == self.STATE_APPROVAL and self._requires_payment_step(applied_cost):
                self.state = self.STATE_PAYMENT
                self._save_to_request()
                return self._reload_wizard()

            if not self._requires_payment_step(applied_cost):
                if not self._meets_next_conditions(applied_cost):
                    raise ValidationError(_(
                        'To continue, either confirm the termination cost is paid (and manager approval is present when the amount is under $100), approve a waiver with $0 applied cost, or approve a reduction and confirm the reduced cost is paid.'
                    ))
                self.state = self.STATE_EQUIPMENT
            else:
                self.state = self.STATE_PAYMENT
                self._save_to_request()
                return self._reload_wizard()
        elif self.state == self.STATE_PAYMENT:
            self._validate_payment_for_cost(applied_cost)
            self.state = self.STATE_EQUIPMENT
        elif self.state == self.STATE_EQUIPMENT:
            self.state = self.STATE_CLOSURE
        self._save_to_request()
        return self._reload_wizard()

    def action_back_to_cost(self):
        self.ensure_one()
        if self.state == self.STATE_CLOSURE:
            self.state = self.STATE_EQUIPMENT
        elif self.state == self.STATE_EQUIPMENT:
            if self._requires_payment_step(self.applied_termination_cost if self.applied_termination_cost is not None else self.early_termination_cost or 0.0):
                self.state = self.STATE_PAYMENT
            else:
                self.state = self.STATE_APPROVAL if self.requires_manager_approval else self.STATE_COST
        elif self.state == self.STATE_PAYMENT:
            self.state = self.STATE_APPROVAL
        elif self.state == self.STATE_APPROVAL:
            self.state = self.STATE_COST
        self._save_to_request()
        return self._reload_wizard()

    def action_abandon(self):
        self.ensure_one()
        request = self.request_id
        if request and request.state == request.STATE_OPEN:
            request.unlink()
        return {'type': 'ir.actions.act_window_close'}

    def action_manager_approve(self):
        self.ensure_one()
        if not (self.env.user.has_group('contract_management.group_contract_management_manager') or
                self.env.user.has_group('base.group_system')):
            raise ValidationError(_('Only a contract manager can approve this termination. If you are not a manager, please ask one to continue.'))
        if self.cost_override_requested and not self.cost_override_applied:
            raise ValidationError(_('Approve or deny the waiver request before continuing.'))

        self._ensure_manager_is_not_requester()
        self.manager_approved = True
        self.manager_user_id = self.env.user
        # Move forward automatically once approved
        if self._requires_payment_step(self.applied_termination_cost if self.applied_termination_cost is not None else self.early_termination_cost or 0.0):
            self.state = self.STATE_PAYMENT
        else:
            self.state = self.STATE_EQUIPMENT if not self.requires_manager_approval else self.STATE_EQUIPMENT
        self._save_to_request()
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
        applied_cost = self.applied_termination_cost if self.applied_termination_cost is not None else self.early_termination_cost or 0.0
        cost_changed = float_compare(applied_cost or 0.0, self.early_termination_cost or 0.0, precision_digits=2) != 0
        if cost_changed and not self.cost_override_applied:
            raise ValidationError(_('A supervisor must apply and approve the cost override before confirming termination.'))
        if self.cost_override_requested and not self.cost_override_applied:
            raise ValidationError(_('A supervisor must approve the requested override or waiver before confirming termination.'))

        if float_compare(applied_cost or 0.0, 0.0, precision_digits=2) == 1:
            self._validate_payment_for_cost(applied_cost)
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

        payment = self.payment_id if float_compare(applied_cost or 0.0, 0.0, precision_digits=2) == 1 else False

        self.contract_id._terminate_with_checks(
            payment_confirmed=float_compare(applied_cost or 0.0, 0.0, precision_digits=2) == 1,
            equipment_returned=True,
            via_wizard=True,
            payment=payment,
            applied_termination_cost=applied_cost,
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