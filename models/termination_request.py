from odoo import api, fields, models


class ContractTerminationRequest(models.Model):
    _name = 'contract.termination.request'
    _description = 'Contract Termination Request'
    _order = 'id desc'

    STATE_OPEN = 'open'
    STATE_DONE = 'done'

    WIZARD_STATES = [
        ('cost', 'Early Termination Cost'),
        ('approval', 'Manager Approval'),
        ('payment', 'Payment Confirmation'),
        ('equipment', 'Equipment Return'),
        ('closure', 'Information Collection'),
    ]

    contract_id = fields.Many2one('contract.management', required=True, index=True)
    subscription_id = fields.Many2one(related='contract_id.subscription_id', store=False, readonly=True)
    partner_id = fields.Many2one(related='contract_id.partner_id', store=False, readonly=True)

    applied_termination_cost = fields.Float()
    cost_override_requested = fields.Boolean()
    cost_override_request_reason = fields.Text()
    cost_override_request_user_id = fields.Many2one('res.users')
    cost_override_request_datetime = fields.Datetime()
    cost_override_attachment_ids = fields.Many2many(
        'ir.attachment',
        'contract_term_request_override_ir_attachment_rel',
        'request_id',
        'attachment_id',
        string='Supporting Documents',
    )
    cost_override_reason = fields.Text()
    cost_override_applied = fields.Boolean()
    cost_override_user_id = fields.Many2one('res.users')
    cost_override_datetime = fields.Datetime()

    payment_id = fields.Many2one('account.payment')
    payment_confirmed = fields.Boolean()
    customer_requests_waiver = fields.Boolean()

    equipment_returned = fields.Boolean()
    manager_approved = fields.Boolean()
    manager_user_id = fields.Many2one('res.users')

    wizard_state = fields.Selection(WIZARD_STATES, default='cost')
    state = fields.Selection(
        [(STATE_OPEN, 'Open'), (STATE_DONE, 'Done')],
        default=STATE_OPEN,
        required=True,
    )

    reason = fields.Many2one('sale.order.close.reason')
    notes = fields.Char()
    other_reason = fields.Char()
    accepted_better_offer = fields.Boolean()
    carrier = fields.Many2one('subscription.competitor')
    bandwidth = fields.Integer()
    upload = fields.Integer()
    tv_included = fields.Boolean()
    telephone_included = fields.Boolean()
    monthly_payment = fields.Float()
    service_rating = fields.Selection([
        ('1', '1 - Very Poor'),
        ('2', '2 - Poor'),
        ('3', '3 - Average'),
        ('4', '4 - Good'),
        ('5', '5 - Excellent'),
    ])
    problems_experienced = fields.Many2many('subscription.problem', string='Problems Experienced')

    def mark_done(self):
        for request in self:
            request.state = self.STATE_DONE
