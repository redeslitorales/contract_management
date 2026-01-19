from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools.misc import html_escape
from odoo.tools import float_round
from markupsafe import Markup
from datetime import date, timedelta
import hmac
import hashlib
from dateutil.relativedelta import relativedelta
import time, base64, uuid, re, json, jwt, requests
import logging

_logger = logging.getLogger(__name__)


SUBSCRIPTION_DRAFT_STATE = ['1_draft', '2_renewal', '7_upsell']

SUBSCRIPTION_STATES = [
    ('1_draft', 'Quotation'),  # Quotation for a new subscription
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

CONTRACT_STATES = [
    ('pending_contract', 'Pending Contract'),
    ('pending_customer_signature', 'Pending Customer Signature'),
    ('pending_cabal_signature', 'Pending Cabal Signature'),
    ('active', 'Active'),
    ('expired', 'Expired'),
    ('terminated', 'Terminated'),
    ('not_required', 'Not Required'),
]

INTERNET_SERVICE_STATES = [
    ('not_active', 'Not Active'),
    ('active', 'Active'),
    ('suspended', 'Suspended'),
    ('paused', 'Paused'),
    ('terminated', 'Terminated'),
]

INSTALLATION_STATES = [
    ('to_be_scheduled', 'To Be Scheduled'),
    ('scheduled', 'Scheduled'),
    ('completed', 'Completed'),
]

SERVICE_CHANGE_MODES = [
    ('no_change', 'No Change'),
    ('config_only', 'Config Change Only'),
    ('activation_only', 'Activation Only'),
    ('install_no_activation', 'Install Without Activation'),
    ('install_with_activation', 'Install with Activation'),
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
    contract_send_method = fields.Selection(string='Send Method', selection=CONTRACT_SEND_METHODS, default='whatsapp', required=True)
    contract_state = fields.Selection(
        selection=CONTRACT_STATES,
        string='Contract State',
        default='pending_contract',
        tracking=True,
        help='Lifecycle state of the contract linked to this sale order.',
    )
    internet_service_state = fields.Selection(
        selection=INTERNET_SERVICE_STATES,
        string='Internet Service State',
        default='not_active',
        tracking=True,
        help='Operational status of the customer internet service for this order.',
    )
    iptv_service_state = fields.Selection(
        selection=INTERNET_SERVICE_STATES,
        string='IPTV Service State',
        default='not_active',
        tracking=True,
        help='Operational status of the customer IPTV service for this order.',
    )
    installation_state = fields.Selection(
        selection=INSTALLATION_STATES,
        string='Installation State',
        default='to_be_scheduled',
        tracking=True,
    )
    configuration_state = fields.Selection(
        selection=INSTALLATION_STATES,
        string='Configuration State',
        default='to_be_scheduled',
        tracking=True,
    )
    service_change_mode = fields.Selection(
        selection=SERVICE_CHANGE_MODES,
        string='Service Change Mode',
        default='no_change',
        tracking=True,
        help='Indicates whether this order triggers install, activation, or config-only work.',
    )
    install_required = fields.Boolean(string='Install Required', default=False, tracking=True)
    activation_required = fields.Boolean(string='Activation Required', default=False, tracking=True)
    subscription_state = fields.Selection(
        string='Subscription Status',
        selection=SUBSCRIPTION_STATES,
        compute='_compute_subscription_state', store=True, tracking=True, group_expand='_group_expand_states',
    )
    contract_ids = fields.One2many('contract.management', 'subscription_id', string="Contracts")
    contract_count = fields.Integer(string='Contract Count', compute='_compute_contract_count')
    docusign_ids = fields.One2many('docusign.connector', 'sale_id', string="DocuSign Envelopes")
    can_resend_contract = fields.Boolean(string='Can Resend Contract', compute='_compute_can_resend_contract')
    
    # FSM Integration - Commented out until FSM modules installed in test env
    # fsm_task_ids = fields.One2many('project.task', 'sale_order_id', string="Install Tasks", 
    #                                 domain=[('is_fsm', '=', True)])
    # fsm_task_count = fields.Integer(string='Install Task Count', compute='_compute_fsm_task_count')
    # next_action = fields.Char(string='Next Action', compute='_compute_next_action')
    transfer_date = fields.Date(string="Date of Transfer")
    transfer_reason = fields.Selection(string="Transfer Reason", selection=TRANSFER_REASONS)
    previous_partner_id = fields.Many2one('res.partner', string="Previous Client")
    terms_conditions_ids = fields.Many2many('sale.terms.conditions', string='Terms and Conditions')
    cover_letter_id = fields.Many2one('sale.cover.letter', string='Cover Letter', compute='_compute_cover_letter', store=True)
    confirmation_uuid = fields.Char(string='UUID', readonly=True, default=lambda self: str(uuid.uuid4()))
    confirmation_url = fields.Char(string='Confirmation URL', compute='_compute_confirmation_url')
    clause_ids = fields.Many2many('contract.clause', string='Clauses')
    quote_confirmed = fields.Boolean(string='Quote Confirmed', default=False)
    contract_term = fields.Many2one('dte.base.contract', string="Contract Term")
    contract_value = fields.Float(string = "Contract Value")
    renewal_of_id = fields.Many2one(
        'sale.order',
        string='Renewal Of',
        help='Source subscription from which this renewal was initiated.',
        copy=False,
        readonly=True,
    )
    upsell_from_id = fields.Many2one(
        'sale.order',
        string='Upsell From',
        help='Source subscription from which this upsell was initiated.',
        copy=False,
        readonly=True,
    )
    progress_stage = fields.Char(
        string='Progress Stage',
        compute='_compute_progress_stage',
        store=False,
        help='Determines which stage to show in the progress bar based on subscription state'
    )
    
    @api.depends('confirmation_uuid')
    def _get_confirmation_secret(self):
        """Return HMAC secret for confirmation links; blank means signing is disabled."""
        ICP = self.env['ir.config_parameter'].sudo()
        return ICP.get_param('contract_management.confirm_secret', '')

    def _sign_confirmation_payload(self, uuid_str, exp_str):
        secret = self._get_confirmation_secret()
        if not secret:
            return ''
        payload = f"{uuid_str}:{exp_str}"
        return hmac.new(secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()

    def _compute_confirmation_url(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        today = fields.Date.context_today(self)
        for order in self:
            uuid_str = order.confirmation_uuid or ''
            exp_date = order.validity_date or (today + relativedelta(days=30))
            exp_str = exp_date.isoformat()
            sig = self._sign_confirmation_payload(uuid_str, exp_str)

            url = f"{base_url}/webhook/confirm_sale_order?uuid={uuid_str}&exp={exp_str}"
            if sig:
                url += f"&sig={sig}"
            order.confirmation_url = url

    @api.depends(
        'subscription_state',
        'contract_state',
        'installation_state',
        'configuration_state',
        'internet_service_state',
        'quote_confirmed',
    )
    def _compute_progress_stage(self):
        """Compute the progress stage to display in the progress bar."""
        for order in self:
            if not order.subscription_state:
                order.progress_stage = 'draft'
                continue
            
            sub_state = order.subscription_state
            contract_state = order.contract_state
            quote_confirmed = order.quote_confirmed

            # Upsell flow only shows draft/confirmed on the progress bar
            if sub_state == '7_upsell':
                if quote_confirmed or order.state in ('sale', 'done'):
                    order.progress_stage = 'confirmed'
                else:
                    order.progress_stage = 'draft'
                continue
            
            # Confirmed: (quotation confirmed, waiting for contract)
            if quote_confirmed:
                # Paused/Suspended with issues should override contract-sign steps
                if sub_state == '4_paused' and (
                    contract_state != 'active'
                    or order.installation_state != 'completed'
                    or order.configuration_state != 'completed'
                ):
                    order.progress_stage = 'paused_with_issues'
                    continue

                if sub_state == '8_suspend' and (
                    contract_state != 'active'
                    or order.installation_state != 'completed'
                    or order.configuration_state != 'completed'
                ):
                    order.progress_stage = 'suspended_with_issues'
                    continue

                # Active with issues should also override contract-sign steps
                if sub_state == '3_progress' and (
                    contract_state != 'active'
                    or order.installation_state != 'completed'
                    or order.configuration_state != 'completed'
                    or order.internet_service_state != 'active'
                ):
                    order.progress_stage = 'active_with_issues'
                    continue

                # Pending contract: contract_state pending_contract
                if contract_state == 'pending_contract':
                    order.progress_stage = 'pending_contract'
                # Pending client signature: contract_state pending_customer_signature
                elif contract_state == 'pending_customer_signature':
                    order.progress_stage = 'pending_client_signature'
                # Pending Cabal signature: contract_state pending_cabal_signature
                elif contract_state == 'pending_cabal_signature':
                    order.progress_stage = 'pending_cabal_signature'
                # Schedule install: installation_state to_be_scheduled
                elif order.installation_state == 'to_be_scheduled' or order.configuration_state == 'to_be_scheduled':
                    order.progress_stage = 'schedule_install'
                # Pending install: installation_state scheduled or pending_install
                elif order.installation_state =='scheduled' or order.configuration_state == 'scheduled':
                    order.progress_stage = 'pending_install'
                # Contract is active and install/config are done; awaiting activation flagging
                elif (
                    contract_state == 'active'
                    and order.installation_state == 'completed'
                    and order.configuration_state == 'completed'
                ):
                    if order.renewal_of_id and order.service_change_mode == 'no_change':
                        order.progress_stage = 'active'
                    # Move to active only when the service itself is active; otherwise stay pending activation
                    elif order.internet_service_state == 'active':
                        order.progress_stage = 'active'
                    else:
                        order.progress_stage = 'pending_activation'
                # Active with issues: subscription says active but contract/install/config/service not all good
                elif sub_state == '3_progress' and (
                    contract_state != 'active'
                    or order.installation_state != 'completed'
                    or order.configuration_state != 'completed'
                    or order.internet_service_state != 'active'
                ):
                    order.progress_stage = 'active_with_issues'
                # Renewed: 5_renewed
                elif sub_state == '5_renewed':
                    order.progress_stage = 'renewed'
                # Active: 3_progress (NOT 4_paused - that's handled separately)
                elif sub_state == '3_progress':
                    order.progress_stage = 'active'
                # Paused: 4_paused (flag issues when contract/install/config not completed)
                elif sub_state == '4_paused' and (
                    contract_state != 'active'
                    or order.installation_state != 'completed'
                    or order.configuration_state != 'completed'
                ):
                    order.progress_stage = 'paused_with_issues'
                elif sub_state == '4_paused':
                    order.progress_stage = 'paused'
                # Suspended: 8_suspend (flag issues when contract/install/config not completed)
                elif sub_state == '8_suspend' and (
                    contract_state != 'active'
                    or order.installation_state != 'completed'
                    or order.configuration_state != 'completed'
                ):
                    order.progress_stage = 'suspended_with_issues'
                elif sub_state == '8_suspend':
                    order.progress_stage = 'suspended'
                # Churned: 6_churn
                elif sub_state == '6_churn':
                    order.progress_stage = 'churned'
                else:
                    order.progress_stage = 'confirmed'
            else:
                order.progress_stage = 'draft'

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
        'subscription_state',
    )
    def _compute_contract_template(self):
        for order in self:
            # For upsells, use addendum template instead of full contract
            # Check both subscription_state (for new upsells) and addendum existence (for processed upsells)
            is_upsell = order.subscription_state == '7_upsell'
            has_addendum = self.env['contract.addendum'].search_count([
                ('upsell_subscription_id', '=', order.id)
            ]) > 0
            
            if is_upsell or has_addendum:
                addendum_report = self.env.ref('contract_management.action_report_contract_addendum_es', raise_if_not_found=False)
                order.contract_template = addendum_report
                _logger.info("[Template] Order %s using addendum template (is_upsell=%s, has_addendum=%s)", 
                           order.name, is_upsell, has_addendum)
                continue
            
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

    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)

        for order in orders:
            if order.subscription_state != '2_renewal' and not order.renewal_of_id:
                continue

            parent = order.renewal_of_id
            if not parent:
                continue

            parent_signature = self._get_product_signature(parent)
            renewal_signature = self._get_product_signature(order)
            target_mode = 'no_change' if renewal_signature == parent_signature else 'install_no_activation'

            if order.service_change_mode != target_mode:
                order.service_change_mode = target_mode

        return orders

    def _get_product_signature(self, order):
        lines = order.order_line.filtered(lambda l: not l.display_type and l.product_id)
        precision = order.env['decimal.precision'].precision_get('Product Unit of Measure') or 6
        return sorted(
            (
                line.product_id.id,
                float_round(line.product_uom_qty, precision_digits=precision),
                line.product_uom.id,
            )
            for line in lines
        )

    @api.depends('contract_ids')
    def _compute_contract_count(self):
        for order in self:
            order.contract_count = len(order.contract_ids)
    
    # FSM Integration - Commented out until FSM modules installed in test env
    # @api.depends('fsm_task_ids')
    # def _compute_fsm_task_count(self):
    #     for order in self:
    #         order.fsm_task_count = len(order.fsm_task_ids)
    # 
    # @api.depends('subscription_state', 'fsm_task_ids', 'fsm_task_ids.stage_id')
    # def _compute_next_action(self):
    #     """Compute the next contextual action based on subscription state and FSM task status"""
    #     for order in self:
    #         if order.installation_state == 'pending_install':
    #             if not order.fsm_task_ids:
    #                 order.next_action = 'create_task'
    #             else:
    #                 # Check if task is scheduled (has planned_date_begin)
    #                 unscheduled = order.fsm_task_ids.filtered(
    #                     lambda t: not t.planned_date_begin and t.stage_id.name not in ['Done', 'Cancelled']
    #                 )
    #                 if unscheduled:
    #                     order.next_action = 'schedule_task'
    #                 else:
    #                     # Check if any task is done
    #                     done_tasks = order.fsm_task_ids.filtered(lambda t: t.stage_id.name == 'Done')
    #                     if done_tasks and not order.cpe_unit_asset:
    #                         order.next_action = 'activate_service'
    #                     else:
    #                         order.next_action = False
    #         else:
    #             order.next_action = False

    @api.depends('contract_state', 'contract_ids', 'docusign_ids')
    def _compute_can_resend_contract(self):
        for order in self:
            order.can_resend_contract = (
                order.contract_state == 'pending_customer_signature'
                and order.contract_ids
                and order.docusign_ids
            )
    
    def action_view_contracts(self):
        """Smart button action to view contracts"""
        self.ensure_one()
        action = self.env.ref('contract_management.action_contract_management').sudo().read()[0]
        contracts = self.contract_ids
        if len(contracts) == 1:
            action['views'] = [(self.env.ref('contract_management.view_contract_management_form').id, 'form')]
            action['res_id'] = contracts.id
        else:
            action['domain'] = [('id', 'in', contracts.ids)]
        action['context'] = {'default_subscription_id': self.id}
        return action
    
    # FSM Integration - Commented out until FSM modules installed in test env
    # def action_view_fsm_tasks(self):
    #     """Smart button action to view install tasks"""
    #     self.ensure_one()
    #     action = self.env['ir.actions.act_window']._for_xml_id('industry_fsm.project_task_action_fsm')
    #     tasks = self.fsm_task_ids
    #     if len(tasks) == 1:
    #         action['views'] = [(False, 'form')]
    #         action['res_id'] = tasks.id
    #     else:
    #         action['domain'] = [('id', 'in', tasks.ids)]
    #     action['context'] = {
    #         'default_partner_id': self.partner_id.id,
    #         'default_sale_order_id': self.id,
    #     }
    #     return action
    # 
    # def action_next_step(self):
    #     """Dynamic action button that changes based on subscription state"""
    #     self.ensure_one()
    #     if self.next_action == 'create_task':
    #         return self.action_create_install_task()
    #     elif self.next_action == 'schedule_task':
    #         return self.action_schedule_install_task()
    #     elif self.next_action == 'activate_service':
    #         # Use existing activation wizard
    #         return self.env['activation.wizard'].with_context(
    #             active_model='sale.order',
    #             active_ids=[self.id],
    #             active_id=self.id
    #         ).action_start()
    #     else:
    #         raise UserError(_("No action available for current state."))
    
    def action_create_install_task(self):
        """Create install task for the subscription based on product category"""
        self.ensure_one()
        
        # Get first subscription product category
        subscription_product = self.order_line.filtered(lambda l: l.product_id).mapped('product_id')[:1]
        if not subscription_product:
            raise UserError(_("No product found on subscription order."))
        
        product_category = subscription_product.categ_id
        
        # Search for installation task types matching the product category
        task_types = self.env['fsm.task.type'].search([
            ('is_installation', '=', True),
            ('subscription_category_ids', 'in', product_category.ids)
        ])
        
        if not task_types:
            raise UserError(_(
                "No installation task type found for product category '%s'.\n\n"
                "Please configure an installation task type with:\n"
                "- 'Is Installation' flag checked\n"
                "- Subscription category matching '%s'"
            ) % (product_category.name, product_category.name))
        
        task_type = task_types[0]
        
        # Create task without scheduling (no date)
        task_vals = {
            'name': _('Installation for %s') % self.partner_id.name,
            'partner_id': self.partner_id.id,
            'sale_order_id': self.id,
            'fsm_task_type_id': task_type.id,
            'project_id': task_type.project_id.id,
        }
        
        # Add default stage if configured
        if task_type.default_stage_id:
            task_vals['stage_id'] = task_type.default_stage_id.id
        
        task = self.env['project.task'].create(task_vals)
        
        # Update subscription state to show schedule button
        # Only change state if we're at pending signature stage (don't move backwards)
        if self.contract_state in ['pending_customer_signature', 'pending_cabal_signature']:
            self.write({'installation_state': 'to_be_scheduled'})
        
        # Return message with link to task
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Installation Task Created'),
                'message': _('Installation task created successfully. Click "Schedule Install" to schedule it.'),
                'type': 'success',
                'sticky': False,
                'next': {
                    'type': 'ir.actions.act_window',
                    'res_model': 'sale.order',
                    'res_id': self.id,
                    'views': [(False, 'form')],
                    'target': 'current',
                }
            }
        }
    
    def action_schedule_install_task(self):
        """Open the FSM intake wizard to schedule the installation task"""
        self.ensure_one()
        
        # Find installation task for this order (scheduled or unscheduled)
        task = self.env['project.task'].search([
            ('sale_order_id', '=', self.id),
            ('fsm_task_type_id.is_installation', '=', True)
        ], limit=1)
        
        if not task:
            raise UserError(_(
                "No installation task found.\n\n"
                "Please create an installation task first."
            ))
        
        # Open FSM intake wizard in reschedule mode
        # The wizard will automatically set state='schedule' when reschedule_task_id is in context
        wizard_view = self.env.ref('fsm_guided_intake.fsm_task_intake_wizard_form', raise_if_not_found=False)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Schedule Installation'),
            'res_model': 'fsm.task.intake.wizard',
            'view_mode': 'form',
            'view_id': wizard_view.id if wizard_view else False,
            'target': 'new',
            'context': {
                'reschedule_task_id': task.id,
            }
        }
    
    # def action_schedule_install_task(self):
    #     """Reschedule existing unscheduled task"""
    #     self.ensure_one()
    #     unscheduled = self.fsm_task_ids.filtered(
    #         lambda t: not t.planned_date_begin and t.stage_id.name not in ['Done', 'Cancelled']
    #     )
    #     if not unscheduled:
    #         raise UserError(_("No unscheduled tasks found."))
    #     
    #     task = unscheduled[0]
    #     
    #     # Open FSM intake wizard in reschedule mode
    #     return {
    #         'type': 'ir.actions.act_window',
    #         'name': _('Reschedule Installation'),
    #         'res_model': 'fsm.task.intake.wizard',
    #         'view_mode': 'form',
    #         'target': 'new',
    #         'context': {
    #             'reschedule_task_id': task.id,
    #             'default_state': 'schedule',
    #         }
    #     }

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

    def _complete_config_changes(self):
        """Placeholder hook: more processing to come."""
        self.with_context(skip_renewal_completion=True).write({
            'installation_state': 'completed',
            'configuration_state': 'completed',
        })

    def signed_manually(self):
        if self.contract_state == 'pending_customer_signature' and self.contract_send_method == 'physical':
            # Auto-create installation task
            try:
                self.action_create_install_task()
            except Exception as e:
                _logger.warning("Failed to auto-create install task: %s", str(e))
                # If task creation fails, still advance state manually
                self.write({'installation_state': 'to_be_scheduled'})
        else:
            raise UserError('Error: No esta firmado fisicamente.')

#    def generate_cover_letter(self):
#        for order in self:
#            cover_letter_template = self.env.ref('your_module.cover_letter_template')
#            cover_letter_html = cover_letter_template._render({
#                'doc': order,
#            }, engine='ir.qweb')
#            order.cover_letter_id.cover_letter = cover_letter_html

    def manually_signed(self):
        # Auto-create installation task when manually marked as signed
        try:
            self.action_create_install_task()
        except Exception as e:
            _logger.warning("Failed to auto-create install task: %s", str(e))
            # If task creation fails, still advance state
            self.write({'installation_state': 'to_be_scheduled'})

    def write(self, vals):
        previous_stage = {order.id: order.progress_stage for order in self}
        previous_contract_state = {order.id: order.contract_state for order in self}

        res = super().write(vals)

        if self.env.context.get('skip_renewal_completion'):
            return res

        contract_state_updated_to_active = vals.get('contract_state') == 'active'
        if contract_state_updated_to_active:
            for order in self:
                was_active = previous_contract_state.get(order.id) == 'active'
                is_active_now = order.contract_state == 'active'
                if was_active or not is_active_now:
                    continue

                is_upsell = order.subscription_state == '7_upsell' or bool(order.upsell_from_id)
                is_renewal = order.subscription_state == '2_renewal' or bool(order.renewal_of_id)

                if (is_upsell or is_renewal) and order.service_change_mode == 'config_only':
                    order._complete_config_changes()

        state_fields_touched = any(key in vals for key in ['contract_state', 'installation_state', 'configuration_state'])
        if state_fields_touched:
            today = fields.Date.context_today(self)
            for order in self:
                if not (order.renewal_of_id or order.subscription_state == '2_renewal'):
                    continue

                all_active_and_done = (
                    order.contract_state == 'active'
                    and order.installation_state == 'completed'
                    and order.configuration_state == 'completed'
                )
                if not all_active_and_done:
                    continue

                updates = {}
                if order.subscription_state != '2_renewal':
                    if order.next_invoice_date and order.next_invoice_date < today:
                        updates['next_invoice_date'] = today
                    if updates:
                        order.with_context(skip_renewal_completion=True).write(updates)
                    order.action_confirm()                      

                    if order.renewal_of_id:
                        parent_updates = {
                            'subscription_state': '5_renewed',
                            'end_date': today,
                        }
                        order.renewal_of_id.with_context(skip_renewal_completion=True).write(parent_updates)

        return res

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

    def _ensure_docusign_config(self):
        params = self.env['ir.config_parameter'].sudo()
        required = {
            'docusign_client_id': params.get_param('docusign_client_id'),
            'docusign_user_id': params.get_param('docusign_user_id'),
            'docusign_private_key': params.get_param('docusign_private_key'),
            'contract_management.docusign_company_signer_email': params.get_param('contract_management.docusign_company_signer_email'),
        }
        missing = [key for key, val in required.items() if not val]
        if missing:
            raise UserError(_("DocuSign configuration is incomplete: missing %s") % ", ".join(missing))
                
    # Method to be used in case a contract needs to be transferred
    def action_subscription_transfer_wizard(self):
        if not self:
            raise ValueError("Expected singleton: sale.order()")
        self.ensure_one()
        if self.is_subscription or self.subscription_state == '7_upsell':
            self._ensure_docusign_config()
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
        if self.is_subscription or self.subscription_state == '7_upsell':
            self._ensure_docusign_config()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'contract.send.method.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_send_method': self.contract_send_method}
        }

    def action_confirm_via_uuid(self):
        _logger.info("[UUID] ===== action_confirm_via_uuid called for order %s (ID: %s) =====", self.name, self.id)
        _logger.info("[UUID] Current subscription_state: %s, is_subscription: %s, origin_order_id: %s", 
                     self.subscription_state, self.is_subscription, self.origin_order_id.name if self.origin_order_id else None)
        initial_subscription_state = self.subscription_state
        
        # Authenticate using hardcoded user 196 (contratos@cabal.sv) for token consistency
        self._ensure_docusign_config()
        authenticated = self.authenicate_jwt()
        user = self.env['res.users'].browse(196)
        if authenticated:
            _logger.info("[UUID] Authentication successful for order %s", self.name)
            
            # Treat as upsell only when explicitly flagged; any renewal_of_id or origin without upsell marker is renewal
            explicit_upsell = initial_subscription_state == '7_upsell' or bool(self.upsell_from_id)
            is_renewal = (
                initial_subscription_state == '2_renewal'
                or bool(self.renewal_of_id)
                or (self.origin_order_id and not explicit_upsell)
            )
            is_upsell = explicit_upsell and not is_renewal
            _logger.info(
                "[Addendum] Upsell detection (UUID) order=%s state=%s upsell_from_id=%s origin_order_id=%s -> is_upsell=%s",
                self.name,
                initial_subscription_state,
                bool(self.upsell_from_id),
                bool(self.origin_order_id),
                is_upsell,
            )
            if is_upsell:
                _logger.info("[Addendum] Upsell detected via UUID for order %s - will create addendum after confirmation", self.name)
            
            # res = super(SaleSubscription, self).action_confirm()
            res = self.action_update_sub_data()
            _logger.info("[UUID] action_update_sub_data() completed for order %s", self.name)
            
            if self.is_subscription or is_upsell:
                _logger.info("[UUID] Order %s is a subscription, processing contract workflow", self.name)
                
                # Create addendum if this was an upsell (checked BEFORE super().action_confirm())
                if is_upsell:
                    _logger.info("[Addendum] Creating addendum for order %s (was upsell)", self.name)
                    try:
                        result = self._create_addendum_for_upsell()
                        _logger.info("[Addendum] Successfully created addendum for order %s", self.name)
                    except Exception as e:
                        _logger.exception("[Addendum] ERROR creating addendum for order %s: %s", self.name, str(e))
                        raise
                
                # NOW move to contract phase (for both upsells and normal subscriptions)
                if is_upsell:
                    parent_subscription = self.origin_order_id
                    if parent_subscription:
                        parent_subscription.write({'contract_state': 'pending_contract'})
                        _logger.info("[Addendum] Set parent subscription %s to pending_contract for upsell order %s", parent_subscription.name, self.name)
                    else:
                        _logger.warning("[Addendum] No parent subscription found to set pending_contract for upsell order %s", self.name)
                else:
                    self.write({'contract_state': 'pending_contract'})
                    _logger.info("[Addendum] Set contract_state to pending_contract for order %s", self.name)
#               if is_renewal and not is_upsell and self.subscription_state != '3_progress':
#                   self.write({'subscription_state': '3_progress'})
                
                # Send for signature (will use addendum template for upsells, full contract for normal subscriptions)
                # Normalize WhatsApp only when the send method is WhatsApp and a number is present
                if self.contract_send_method == 'whatsapp':
                    whatsapp_number = self.partner_id.whatsapp or ''
                    if not isinstance(whatsapp_number, str) or not whatsapp_number:
                        self.write({'contract_send_method': 'email'})
                    else:
                        if whatsapp_number.startswith('+1') and len(whatsapp_number) == 12:
                            match = re.match(r'^\+(\d{1})(\d{10})$', whatsapp_number)
                        elif whatsapp_number.startswith('+503') and len(whatsapp_number) == 12:
                            match = re.match(r'^\+(\d{1,3})(\d+)$', whatsapp_number)
                        else:
                            match = re.match(r'^\+(\d{1,3})(\d{4,14})$', whatsapp_number)
                        if match:
                            country_code = match.group(1)
                            phone_number = match.group(2)
                        else:
                            self.write({'contract_send_method': 'email'})
                self.action_send_for_signature()
            return res
        
#    def action_confirm(self):
#        user = self.env['res.users'].browse(196)
#       user = self.env.user
#        _logger.info("[DEBUG] action_confirm called for order %s, is_subscription=%s, subscription_state=%s", 
#                    self.name, self.is_subscription, self.subscription_state)
#       initial_subscription_state = self.subscription_state
#
#
#        
#        self._ensure_docusign_config()
#        authenticated = self.authenicate_jwt()
#        if authenticated:
#            # Confirm the order first for all subscriptions
#            res = super(SaleSubscription, self).action_confirm()
#            _logger.info("[DEBUG] Super action_confirm completed for order %s", self.name)
#            
#            if self.is_subscription or self.subscription_state == '7_upsell':
#                # Check if this is an upsell FIRST (before changing state!)
#                explicit_upsell = initial_subscription_state == '7_upsell' or bool(self.upsell_from_id)
#                is_renewal = (
#                    initial_subscription_state == '2_renewal'
#                    or bool(self.renewal_of_id)
#                    or (self.origin_order_id and not explicit_upsell)
#                )
#                is_upsell = explicit_upsell and not is_renewal
#                _logger.info(
#                    "[Addendum] Upsell detection order=%s state=%s upsell_from_id=%s origin_order_id=%s -> is_upsell=%s",
#                    self.name,
#                    initial_subscription_state,
#                    bool(self.upsell_from_id),
#                    bool(self.origin_order_id),
#                    is_upsell,
#                )
#                if is_upsell:
#                    _logger.info("[Addendum] Upsell detected for order %s - creating addendum", self.name)
#                    # Create addendum while state is still '7_upsell'
#                    try:
#                        self._create_addendum_for_upsell()
#                        _logger.info("[Addendum] Successfully created addendum for order %s", self.name)
#                    except Exception as e:
#                        _logger.exception("[Addendum] ERROR creating addendum for order %s: %s", self.name, str(e))
#                        raise
#                
#                # NOW move to contract phase (for both upsells and normal subscriptions)
#                if is_upsell:
#                    parent_subscription = self.origin_order_id
#                    if parent_subscription:
#                        parent_subscription.write({'contract_state': 'pending_contract'})
#                        _logger.info("[Addendum] Set parent subscription %s to pending_contract for upsell order %s", parent_subscription.name, self.name)
#                    else:
#                        _logger.warning("[Addendum] No parent subscription found to set pending_contract for upsell order %s", self.name)
#                else:
#                    self.write({'contract_state': 'pending_contract'})
#                    _logger.info("[Addendum] Set contract_state to pending_contract for order %s", self.name)
#                if is_renewal and not is_upsell and self.subscription_state != '3_progress':
#                    self.write({'subscription_state': '3_progress'})
#                
#                # Normal subscription/upsell flow - open wizard to send contract/addendum
 #               return {
 #                   'type': 'ir.actions.act_window',
 #                   'res_model': 'contract.send.method.wizard',
 #                   'view_mode': 'form',
 #                   'target': 'new',
 #                   'context': {'default_send_method': self.contract_send_method}
 #               }
 #           return res
 #       else:
 #           raise ValidationError(_("Failed to obtain access token from DocuSign via JWT. Please review DocuSign configuration."))

    def action_update_sub_data(self):
        user = self.env['res.users'].browse(196)
#       user = self.env.user
        _logger.info("[DEBUG] action_update_sub_data called for order %s, is_subscription=%s, subscription_state=%s", 
                    self.name, self.is_subscription, self.subscription_state)
        initial_subscription_state = self.subscription_state

        self._ensure_docusign_config()
        authenticated = self.authenicate_jwt()
        if authenticated:
            if self.is_subscription or self.subscription_state == '7_upsell':
                # Check if this is an upsell FIRST (before changing state!)
                explicit_upsell = initial_subscription_state == '7_upsell' or bool(self.upsell_from_id)
                is_renewal = (
                    initial_subscription_state == '2_renewal'
                    or bool(self.renewal_of_id)
                    or (self.origin_order_id and not explicit_upsell)
                )
                is_upsell = explicit_upsell and not is_renewal
                _logger.info(
                    "[Addendum] Upsell detection order=%s state=%s upsell_from_id=%s origin_order_id=%s -> is_upsell=%s",
                    self.name,
                    initial_subscription_state,
                    bool(self.upsell_from_id),
                    bool(self.origin_order_id),
                    is_upsell,
                )
                if is_upsell:
                    _logger.info("[Addendum] Upsell detected for order %s - creating addendum", self.name)
                    # Create addendum while state is still '7_upsell'
                    try:
                        self._create_addendum_for_upsell()
                        _logger.info("[Addendum] Successfully created addendum for order %s", self.name)
                    except Exception as e:
                        _logger.exception("[Addendum] ERROR creating addendum for order %s: %s", self.name, str(e))
                        raise
                
                # NOW move to contract phase (for both upsells and normal subscriptions)
                if is_upsell:
                    parent_subscription = self.origin_order_id
                    if parent_subscription:
                        parent_subscription.write({'contract_state': 'pending_contract'})
                        _logger.info("[Addendum] Set parent subscription %s to pending_contract for upsell order %s", parent_subscription.name, self.name)
                    else:
                        _logger.warning("[Addendum] No parent subscription found to set pending_contract for upsell order %s", self.name)
                else:
                    self.write({'contract_state': 'pending_contract'})
                    _logger.info("[Addendum] Set contract_state to pending_contract for order %s", self.name)
                
                # Normal subscription/upsell flow - open wizard to send contract/addendum
                return {
                    'type': 'ir.actions.act_window',
                    'res_model': 'contract.send.method.wizard',
                    'view_mode': 'form',
                    'target': 'new',
                    'context': {'default_send_method': self.contract_send_method}
                }
            return True
        else:
            raise ValidationError(_("Failed to obtain access token from DocuSign via JWT. Please review DocuSign configuration."))


    def _find_parent_contract(self):
        """Find the active contract for the parent subscription (used for upsells)"""
        self.ensure_one()
        if not self.origin_order_id:
            _logger.warning("[Addendum] No parent subscription found for upsell %s", self.name)
            return False
        
        # Find active contract for parent subscription
        parent_contracts = self.env['contract.management'].search([
            ('subscription_id', '=', self.origin_order_id.id),
            ('state', 'in', ['active', 'signed'])
        ], order='create_date desc', limit=1)
        
        if not parent_contracts:
            _logger.warning("[Addendum] No active contract found for parent subscription %s", self.origin_order_id.name)
            return False
        
        return parent_contracts[0]

    def _create_addendum_for_upsell(self):
        """Create and send addendum for upsell order"""
        self.ensure_one()
        _logger.info("[Addendum] _create_addendum_for_upsell called for order %s", self.name)
        
        # Find parent contract
        parent_contract = self._find_parent_contract()
        _logger.info("[Addendum] Parent contract search result: %s", parent_contract.name if parent_contract else "None")
        
        if not parent_contract:
            error_msg = _("Cannot create addendum: No active contract found for parent subscription. "
                            "Please ensure the parent subscription has an active contract.")
            _logger.error("[Addendum] %s", error_msg)
            raise UserError(error_msg)
        
        # Calculate financial impact
        monthly_payment_change = 0.0
        one_time_fee = 0.0
        
        # Calculate change in monthly payment from recurring lines
        recurring_lines = self.order_line.filtered(lambda l: l.product_id.recurring_invoice)
        for line in recurring_lines:
            monthly_payment_change += line.price_total
        
        # Calculate one-time fees from non-recurring lines
        one_time_lines = self.order_line.filtered(lambda l: not l.product_id.recurring_invoice)
        for line in one_time_lines:
            one_time_fee += line.price_total

        # Build description of changes
        description_parts = ["Service additions from upsell:"]
        for line in self.order_line:
            description_parts.append(f"- {line.product_id.name}: {line.product_uom_qty} x ${line.price_unit:.2f}")
        description = "\n".join(description_parts)
        
        # Create addendum
        addendum_vals = {
            'name': f'Upsell Addendum - {self.name}',
            'contract_id': parent_contract.id,
            'upsell_subscription_id': self.id,  # Link to upsell order
            'addendum_type': 'service_addition',
            'description': description,
            'effective_date': fields.Date.today(),
            'state': 'draft',
            'contract_send_method': self.contract_send_method or 'whatsapp',
            'monthly_payment_change': monthly_payment_change,
            'one_time_fee': one_time_fee,
        }
        
        addendum = self.env['contract.addendum'].create(addendum_vals)
        _logger.info("[Addendum] Created addendum %s (ID: %s) for upsell %s", 
                    addendum.name, addendum.id, self.name)
        
        # Link addendum to order's chatter with a rendered hyperlink
        addendum_url = "/web#id=%s&model=contract.addendum&view_type=form" % addendum.id
        # Use Markup so the link renders as HTML while escaping the name
        body_html = Markup(_("Addendum created: <a href='{url}'>{name}</a>"))
        self.message_post(
            body=body_html.format(url=addendum_url, name=html_escape(addendum.name)),
            subject=_("Addendum Created for Upsell"),
            message_type='comment',
            subtype_xmlid='mail.mt_note',
        )
        
        # Return action to open the addendum
        return {
            'type': 'ir.actions.act_window',
            'name': 'Addendum for Upsell',
            'res_model': 'contract.addendum',
            'res_id': addendum.id,
            'view_mode': 'form',
            'target': 'current',
            'context': {
                'form_view_initial_mode': 'edit',
            }
        }

    def action_send_contract(self):
        """Button action to send contract when in pending contract state without docusign connector."""
        self.ensure_one()
        if self.docusign_connector_ids:
            raise UserError(_('Contract has already been sent. Use Resend Contract instead.'))
        if self.contract_state != 'pending_contract':
            raise UserError(_('Contract can only be sent when contract state is Pending Contract.'))
        
        # Open the send method wizard
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'contract.send.method.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_send_method': self.contract_send_method}
        }

    def action_quotation_send(self):
        """Override to conditionally send quotation email automatically.
        
        - Production (web.base.url starts with https://servicio.): Send automatically
        - Test/Dev environments: Open standard email composer dialog
        """
        self.ensure_one()
        
        # Check environment based on web.base.url
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        is_production = base_url.startswith('https://servicio.')
        
        _logger.info("[QuoteSend] action_quotation_send called for order %s (ID: %s), base_url=%s, is_production=%s", 
                    self.name, self.id, base_url, is_production)
        
        # In test/dev environments, use standard email composer
        if not is_production:
            _logger.info("[QuoteSend] Non-production environment detected, opening email composer dialog")
            return super().action_quotation_send()
        
        # Production: Automatic sending
        _logger.info("[QuoteSend] Production environment detected, sending automatically")
        
        # Validate customer has an email
        if not self.partner_id.email:
            _logger.warning("[QuoteSend] Order %s: Customer %s has no email address", 
                          self.name, self.partner_id.name)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('No Email Address'),
                    'message': _('Customer %s has no email address configured.') % self.partner_id.name,
                    'type': 'warning',
                    'sticky': False,
                }
            }
        
        # Get the quotation email template
        template = self.env.ref('sale.email_template_edi_sale', raise_if_not_found=False)
        if not template:
            _logger.error("[QuoteSend] Email template 'sale.email_template_edi_sale' not found")
            raise UserError(_('Quotation email template not found. Please contact administrator.'))
        
        _logger.info("[QuoteSend] Queueing email using template ID=%s to customer %s (%s)", 
                    template.id, self.partner_id.name, self.partner_id.email)
        
        try:
            # Queue the email for async sending (more robust than force_send)
            # force_send=False means it will be queued and sent by mail queue cron
            template.send_mail(self.id, force_send=False, raise_exception=False)
            
            # Mark quotation as sent
            if self.state in ('draft', 'sent'):
                self.write({'state': 'sent'})

            # Log to chatter for production auto-send to keep audit trail
            template_name = template.display_name or template.name or _('quotation template')
            self.message_post(
                body=_('Quotation sent automatically to %s using template %s.') % (self.partner_id.email, template_name),
                subtype_xmlid='mail.mt_note',
                message_type='comment',
            )
            
            _logger.info("[QuoteSend] Email queued successfully for order %s", self.name)
            
            # Return a success notification
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Quotation Queued'),
                    'message': _('Quotation will be sent to %s shortly.') % self.partner_id.email,
                    'type': 'success',
                    'sticky': False,
                }
            }
            
        except Exception as e:
            _logger.exception("[QuoteSend] Failed to queue email for order %s", self.name)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Email Error'),
                    'message': _('Failed to queue quotation email: %s') % str(e),
                    'type': 'danger',
                    'sticky': True,
                }
            }

    def action_send_for_signature(self):
        _logger.info("[DocuSign] action_send_for_signature called for %d contract(s)", len(self))
        for contract in self:
            _logger.info("[DocuSign] Processing contract ID=%s, name=%s, send_method=%s", 
                        contract.id, contract.name, contract.contract_send_method)
            
            # Calculate monthly_payment (tax-inclusive) and contract_value from recurring order lines
            base_order = contract._get_addendum_base_order()
            contract_value_source = contract._get_contract_value_source_order()
            recurring_lines_current = contract.order_line.filtered(lambda l: l.product_id.recurring_invoice)
            recurring_lines_base = base_order.order_line.filtered(lambda l: l.product_id.recurring_invoice) if base_order and base_order != contract else contract.env['sale.order.line']
            combined_recurring_lines = (recurring_lines_base | recurring_lines_current) if recurring_lines_base else recurring_lines_current

            # Use non-upsell ancestor for monthly and contract value to avoid double counting
            contract_value = 0.0
            contract_value_lines = contract_value_source.order_line.filtered(lambda l: l.product_id.recurring_invoice) if contract_value_source else contract.env['sale.order.line']
            contract_value_monthly = sum(line.price_total for line in contract_value_lines)
            monthly_payment = contract_value_monthly

            # Calculate contract value based on contract term and billing period (prefer base order settings for upsells)
            billing_source = base_order if base_order else contract
            contract_value_billing_source = contract_value_source if contract_value_source else billing_source
            if contract_value_billing_source.contract_term and contract_value_billing_source.plan_id:
                contract_term_months = contract_value_billing_source.contract_term.term
                # Get billing period in months from the plan
                billing_period_months = contract_value_billing_source.plan_id.billing_period_value if contract_value_billing_source.plan_id.billing_period_unit == 'month' else 1
                if billing_period_months > 0:
                    duration = contract_term_months / billing_period_months
                    contract_value = contract_value_monthly * duration
                else:
                    contract_value = contract_value_monthly * 12  # Fallback to 12 if calculation fails
            else:
                contract_value = contract_value_monthly * 12  # Default to 12 if no contract term/plan
            
            _logger.info("[DocuSign] Calculated values for contract ID=%s: monthly_payment=%.2f, contract_value=%.2f", 
                        contract.id, monthly_payment, contract_value)

            # Persist contract_value so QWeb reports (contract PDFs) render the correct amount
            contract.sudo().write({'contract_value': contract_value})
            
            # Step 1: Generate contract number
            if contract.is_subscription and not contract.cabal_sequence:
                contract.sudo().cabal_sequence = contract._get_cabal_sequence()
                _logger.info("[DocuSign] Generated contract sequence: %s", contract.cabal_sequence)
            
            # Step 2: Fetch the contract template (force recompute for upsells)
            # For upsells, check if an addendum was created (not subscription_state, which may have changed)
            has_addendum = self.env['contract.addendum'].search_count([
                ('upsell_subscription_id', '=', contract.id)
            ]) > 0
            
            _logger.info("[DocuSign] Contract subscription_state=%s, has_addendum=%s", 
                        contract.subscription_state, has_addendum)

            addendum_record = False
            if has_addendum:
                addendum_record = self.env['contract.addendum'].search([
                    ('upsell_subscription_id', '=', contract.id)
                ], order='create_date desc', limit=1)
            
            if has_addendum:
                # This is an upsell - force recompute to use addendum template
                contract._compute_contract_template()
                _logger.info("[DocuSign] Addendum detected, forced recompute. contract_template=%s", 
                           contract.contract_template.name if contract.contract_template else None)
            
            if not contract.contract_template:
                _logger.error("[DocuSign] Contract template not specified for contract ID=%s", contract.id)
                raise UserError('Contract template not specified.')
            # Step 3: Create the document to be signed using the template
            _logger.info("[DocuSign] Creating document for contract ID=%s with template=%s", 
                        contract.id, contract.contract_template.name)
            document = self._create_document_to_be_signed(contract, contract.contract_template)
            _logger.info("[DocuSign] Document created: ID=%s, name=%s", document.id, document.name)
            
            # Step 4: Create the connector and connector line records (if not physical)
            connector_id = None
            if contract.contract_send_method != 'physical':
                _logger.info("[DocuSign] Creating DocuSign connector for contract ID=%s", contract.id)
                connector_id = self._send_document_to_docusign(contract, document)
                _logger.info("[DocuSign] DocuSign connector created: ID=%s, name=%s", 
                            connector_id.id, connector_id.name)

            # Step 5: Create the contract management record (before sending to ensure logging exists)
            # Reuse latest contract.management if it already exists to avoid duplicates
            k_management = self.env['contract.management'].sudo().search([
                ('subscription_id', '=', contract.id)
            ], order='create_date desc', limit=1)

            new_contract_record = not bool(k_management)
            if new_contract_record:
                _logger.info("[DocuSign] Creating contract.management record for subscription ID=%s", contract.id)
                k_management = self.env['contract.management'].sudo().create({
                    'subscription_id': contract.id,
                    "contract_send_method": contract.contract_send_method,
                    'monthly_payment': monthly_payment,
                    'contract_value': contract_value,
                })
                _logger.info(
                    "[DocuSign] contract.management created: ID=%s with monthly_payment=%.2f, contract_value=%.2f",
                    k_management.id,
                    monthly_payment,
                    contract_value,
                )
            else:
                _logger.info(
                    "[DocuSign] Reusing existing contract.management ID=%s for subscription ID=%s",
                    k_management.id,
                    contract.id,
                )
                k_management.sudo().write({
                    "contract_send_method": contract.contract_send_method,
                    'monthly_payment': monthly_payment,
                    'contract_value': contract_value,
                })
                _logger.info(
                    "[DocuSign] Updated existing contract.management ID=%s with monthly_payment=%.2f, contract_value=%.2f",
                    k_management.id,
                    monthly_payment,
                    contract_value,
                )

            # Step 5a: Create contract service lines from subscription order lines only for new records
            if new_contract_record:
                _logger.info(
                    "[DocuSign] Creating contract service lines for contract.management ID=%s",
                    k_management.id,
                )
                service_lines_created = 0
                for line in contract.order_line:
                    if line.product_id and not line.display_type:
                        self.env['contract.service'].sudo().create({
                            'contract_id': k_management.id,
                            'product_id': line.product_id.id,
                            'name': line.name or line.product_id.name,
                            'price': line.price_total,  # Use price_total to include taxes
                        })
                        service_lines_created += 1
                _logger.info(
                    "[DocuSign] Created %d service lines for contract.management ID=%s",
                    service_lines_created,
                    k_management.id,
                )

            if connector_id:
                write_vals = {'contract_management_id': k_management.id}
                if addendum_record:
                    write_vals['contract_addendum_id'] = addendum_record.id
                connector_id.sudo().write(write_vals)
                _logger.info("[DocuSign] Linked connector %s to contract.management %s%s", 
                             connector_id.id, k_management.id, 
                             f" and addendum {addendum_record.id}" if addendum_record else "")
            
            # Step 6: Link docusign connector to contract.management
            if contract.contract_send_method != 'physical' and connector_id:
                k_management.sudo().write({'docusign_id': connector_id.id})
                _logger.info("[DocuSign] contract.management updated with docusign_id=%s", connector_id.id)
            
            # Step 7: Send document (after local records are created)
            base_order = contract._get_addendum_base_order()
            target_order = base_order if base_order and base_order != contract else contract

            if contract.contract_send_method != 'physical':
                # Send document from Docusign
                _logger.info("[DocuSign] Calling send_docs() with send_method=%s for connector ID=%s", 
                            contract.contract_send_method, connector_id.id)
                send_contract_result = connector_id.send_docs(contract.contract_send_method)
                _logger.info("[DocuSign] send_docs() result: %s", send_contract_result)
                
                # Treat any 2xx-style success or "Successful" result as success
                # DocuSign APIs return 200, 201, 202 (accepted), 204 (no content) as success
                is_success = False
                if isinstance(send_contract_result, dict):
                    # Check for explicit success indicators (use translated comparison)
                    if send_contract_result.get('name') == _("Successful"):
                        is_success = True
                    # Check for HTTP status code (if present) - any 2xx is success
                    elif 'status_code' in send_contract_result:
                        status_code = send_contract_result.get('status_code')
                        if 200 <= status_code < 300:
                            is_success = True
                    # Check for other success indicators
                    elif send_contract_result.get('success') is True:
                        is_success = True
                
                if is_success:
                    _logger.info("[DocuSign] SUCCESS: Contract sent successfully for contract ID=%s", contract.id)
                    
                    # Get envelope ID from first connector line
                    envelope_id = connector_id.connector_line_ids[0].envelope_id if connector_id.connector_line_ids else None
                    
                    # Format method for display
                    if contract.contract_send_method in ['whatsapp', 'email']:
                        method_display = f"DocuSign ({contract.contract_send_method.capitalize()})"
                    else:
                        method_display = contract.contract_send_method.capitalize()
                    
                    # Construct message with envelope ID
                    msg_body = f'SUCCESS: Contract {document.name} sent to customer via {method_display}'
                    if envelope_id:
                        msg_body += f' - Envelope ID: {envelope_id}'
                    
                    target_order.sudo().message_post(body=msg_body, attachment_ids=[document.id])
                    target_order.sudo().write({'contract_state': 'pending_customer_signature'})
                    _logger.info(
                        "[DocuSign] Contract state updated on %s to 'pending_customer_signature'",
                        target_order.name,
                    )

                    # If this was an upsell/addendum, leave a note on the upsell order to look at the parent
                    if target_order != contract:
                        contract.sudo().message_post(body=_("Addendum sent via DocuSign. See parent subscription %s for envelope status and chatter.") % target_order.name)
                else:
                    # Don't use 'name' field as error message - it may contain success indicators
                    error_msg = send_contract_result.get('message') or send_contract_result.get('error') or str(send_contract_result)
                    _logger.error("[DocuSign] Failed to send contract ID=%s. Result: %s", 
                                 contract.id, send_contract_result)
                    target_order.sudo().message_post(body=f'ERROR sending contract {document.name}: {error_msg}')
                    if target_order != contract:
                        contract.sudo().message_post(body=_("Addendum send failed. Check parent subscription %s for details.") % target_order.name)
                    raise ValidationError(f"Failed to send contract via DocuSign: {error_msg}")
            else:
                    _logger.info("[DocuSign] Physical contract method - creating print activity for contract ID=%s", 
                                contract.id)
                    target_order.sudo().message_post(body=f'Contract {document.name} is ready to be printed and signed.', attachment_ids=[document.id])
                    target_order.sudo().write({'contract_state': 'pending_customer_signature'})
                    if target_order != contract:
                        contract.sudo().message_post(body=_("Addendum ready for physical signature. See parent subscription %s for details." ) % target_order.name)
                    target_order.create_print_sign_activity()
    #            contract.write({'contract_management_id': k_management.id})
                # Update the contract with the DocuSign envelope ID
    #            contract.docusign_envelope_id = envelope_id
    #            contract.state = 'signature_in_process'
        return

    def action_resend_contract(self):
        self.ensure_one()

        connector_candidates = self.docusign_ids.filtered(lambda c: c.state == 'sent' and c.connector_line_ids)
        if not connector_candidates:
            raise ValidationError(_("No sent DocuSign envelope found to resend."))

        connector = connector_candidates.sorted(key=lambda c: c.id, reverse=True)[0]

        if any(line.sign_status for line in connector.connector_line_ids):
            raise ValidationError(_("At least one recipient has already signed. Please void and create a new envelope to resend."))

        if not any(connector.connector_line_ids.mapped('envelope_id')):
            raise ValidationError(_("Cannot resend because the envelope ID is missing."))

        if not self.contract_template:
            raise ValidationError(_("Contract template is not specified."))

        if not self.cabal_sequence:
            self.sudo().cabal_sequence = self._get_cabal_sequence()

        contract_value_source = self._get_contract_value_source_order()
        recurring_lines = contract_value_source.order_line.filtered(lambda l: l.product_id.recurring_invoice) if contract_value_source else self.env['sale.order.line']
        monthly_payment = sum(recurring_lines.mapped('price_total'))

        contract_value = 0.0
        billing_source = contract_value_source if contract_value_source else self
        if billing_source.contract_term and billing_source.plan_id:
            contract_term_months = billing_source.contract_term.term
            billing_period_months = billing_source.plan_id.billing_period_value if billing_source.plan_id.billing_period_unit == 'month' else 1
            if billing_period_months > 0:
                duration = contract_term_months / billing_period_months
                contract_value = monthly_payment * duration
            else:
                contract_value = monthly_payment * 12
        else:
            contract_value = monthly_payment * 12

        document = self._create_document_to_be_signed(self, self.contract_template)

        connector.sudo().write({
            'attachment_ids': [(6, 0, [document.id])],
            'monthly_payment': monthly_payment,
            'contract_value': contract_value,
        })

        result = connector.send_docs(self.contract_send_method)

        envelope_id = connector.connector_line_ids[:1].envelope_id if connector.connector_line_ids else False
        msg = f"Contract {document.name} replaced and resent via DocuSign"
        if envelope_id:
            msg += f" - Envelope ID: {envelope_id}"
        self.sudo().message_post(body=msg, attachment_ids=[document.id])

        return result

    def _get_addendum_base_order(self):
        """Return the parent contract/order for addendum rendering."""
        self.ensure_one()
        # Only use parent order for upsells; renewals and base subscriptions use themselves
        is_upsell = self.subscription_state == '7_upsell' or getattr(self, 'upsell_from_id', False)
        if is_upsell and getattr(self, 'origin_order_id', False):
            return self.origin_order_id
        return self

    def _get_contract_value_source_order(self):
        """Walk up the chain to a non-upsell order for contract value math."""
        self.ensure_one()
        order = self._get_addendum_base_order()
        while order and getattr(order, 'upsell_from_id', False):
            order = order.upsell_from_id
        return order

    def _get_recurring_lines_for_addendum(self):
        """Collect recurring lines, preferring the parent order for upsells."""
        self.ensure_one()
        base_order = self._get_addendum_base_order()

        lines = base_order.order_line if getattr(base_order, 'order_line', False) else base_order.env['sale.order.line']
        if not lines:
            lines = base_order.recurring_invoice_line_ids if getattr(base_order, 'recurring_invoice_line_ids', False) else base_order.env['sale.order.line']

        if not lines:
            return base_order.env['sale.order.line']

        return lines.filtered(lambda l: l.product_template_id and getattr(l.product_template_id, 'recurring_invoice', False))

    def _get_addendum_monthly_total(self, recurring_lines=None):
        """Compute monthly total safely, falling back to stored totals."""
        self.ensure_one()
        recurring_lines = recurring_lines if recurring_lines is not None else self._get_recurring_lines_for_addendum()

        if recurring_lines:
            return sum((l.price_unit or 0.0) * (l.product_uom_qty or 1.0) for l in recurring_lines)

        base_order = self._get_addendum_base_order()

        if hasattr(base_order, 'recurring_total'):
            return base_order.recurring_total or 0.0

        if hasattr(base_order, 'amount_total'):
            return base_order.amount_total or 0.0

        return 0.0

    def _create_document_to_be_signed(self, subscription, report_template):
        # Render the report as PDF
        # Check to make sure that the contract template has been specified
        _logger.info("[DocuSign] _create_document_to_be_signed called for subscription ID=%s", subscription.id)
        
        # LOG RECORD DATA FOR DEBUGGING
        _logger.info("[DocuSign] DEBUG - Subscription record data:")
        _logger.info("[DocuSign] - name: %s", subscription.name)
        _logger.info("[DocuSign] - partner_id: %s", subscription.partner_id.name if subscription.partner_id else None)
        _logger.info("[DocuSign] - has order_line: %s", hasattr(subscription, 'order_line'))
        _logger.info("[DocuSign] - order_line count: %s", len(subscription.order_line) if hasattr(subscription, 'order_line') else 'N/A')
        if hasattr(subscription, 'order_line') and subscription.order_line:
            for idx, line in enumerate(subscription.order_line):
                _logger.info("[DocuSign] - order_line[%d]: product=%s, price_unit=%s, product_uom_qty=%s, recurring=%s", 
                            idx, 
                            line.product_id.name if line.product_id else None,
                            line.price_unit,
                            line.product_uom_qty,
                            line.product_template_id.recurring_invoice if hasattr(line, 'product_template_id') and line.product_template_id else 'N/A')
        _logger.info("[DocuSign] - has recurring_invoice_line_ids: %s", hasattr(subscription, 'recurring_invoice_line_ids'))
        _logger.info("[DocuSign] - has recurring_total: %s (value=%s)", hasattr(subscription, 'recurring_total'), getattr(subscription, 'recurring_total', 'N/A'))
        _logger.info("[DocuSign] - has amount_total: %s (value=%s)", hasattr(subscription, 'amount_total'), getattr(subscription, 'amount_total', 'N/A'))
        
        if not report_template:
            _logger.error("[DocuSign] No contract template specified for subscription ID=%s", subscription.id)
            raise ValueError("No contract template specified.")
        # Fetch report action
        _logger.info("[DocuSign] Fetching report action for template ID=%s", report_template.id)
        report_action = report_template.sudo().read()[0]
        # Generate the attachment
        _logger.info("[DocuSign] Rendering PDF for subscription ID=%s using report ID=%s", 
                    subscription.id, report_action['id'])
        pdf_content, _ = self.env['ir.actions.report']._render_qweb_pdf(report_action['id'], [subscription.id])
        pdf_size = len(pdf_content)
        _logger.info("[DocuSign] PDF generated successfully, size=%d bytes", pdf_size)
        # Create an attachment for the generated PDF
        attachment_name = f'{subscription.cabal_sequence}_{subscription.name}_customer_contract.pdf'
        _logger.info("[DocuSign] Creating attachment: %s", attachment_name)
        attachment = self.env['ir.attachment'].sudo().create({
            'name': attachment_name,
            'type': 'binary',
            'datas': base64.b64encode(pdf_content),
            'res_model': 'sale.order',
            'res_id': subscription.id,
            'mimetype': 'application/pdf'
        })
        _logger.info("[DocuSign] Attachment created successfully: ID=%s", attachment.id)
        return attachment

    def _send_document_to_docusign(self, contract, document):
        # Retrieve DocuSign credentials from the custom model
        _logger.info("[DocuSign] _send_document_to_docusign called for contract ID=%s, document ID=%s", 
                    contract.id, document.id)
        
        # Get company signer email from settings
        company_signer_email = self.env['ir.config_parameter'].sudo().get_param(
            'contract_management.docusign_company_signer_email'
        )
        
        if not company_signer_email:
            _logger.error("[DocuSign] Company signer email not configured in settings")
            raise UserError("DocuSign company signer email is not configured. Please configure it in Settings > General Settings > Contract Management.")
        
        # Look up user by login (not email, since login contains the business email)
        user = self.env['res.users'].search([('login', '=', company_signer_email)], limit=1)
        if not user:
            _logger.error("[DocuSign] No user found with login=%s", company_signer_email)
            raise UserError(f"No user found with login {company_signer_email}. Please check the DocuSign company signer email in settings.")
        
        _logger.info("[DocuSign] Retrieved DocuSign user ID=%s, name=%s, email=%s", user.id, user.name, user.email)
        
        if not user.access_token or not user.account_id:
            _logger.error("[DocuSign] DocuSign credentials not configured for user ID=%s (email=%s). "
                         "access_token=%s, account_id=%s", 
                         user.id, user.email, bool(user.access_token), user.account_id)
            raise UserError(f"DocuSign credentials are not configured for user {user.email}.")
        
        # Calculate custom fields for DocuSign envelope
        # Monthly payment = sum of recurring order lines with taxes
        recurring_lines = contract.order_line.filtered(lambda l: l.product_id.recurring_invoice)
        monthly_payment = sum(recurring_lines.mapped('price_total'))

        # Contract value based on contract term and billing period
        # duration = contract_term_months / billing_period_months
        contract_value = 0.0
        if contract.contract_term and contract.plan_id:
            contract_term_months = contract.contract_term.term
            billing_period_months = contract.plan_id.billing_period_value if contract.plan_id.billing_period_unit == 'month' else 1
            if billing_period_months > 0:
                duration = contract_term_months / billing_period_months
                contract_value = monthly_payment * duration
            else:
                contract_value = monthly_payment * 12  # Fallback to 12 if calculation fails
        else:
            contract_value = monthly_payment * 12  # Default to 12 if no contract term/plan

        # Use parent subscription for addendum envelopes so connector points to original contract
        base_order = contract._get_addendum_base_order()
        connector_sale_id = base_order.id if base_order else contract.id

        _logger.info("[DocuSign] Creating docusign.connector record with name=%s, sale_id=%s", 
                    contract.cabal_sequence, connector_sale_id)
        _logger.info("[DocuSign] Custom fields: monthly_payment=%.2f, contract_value=%.2f", monthly_payment, contract_value)
        connector_record = self.env['docusign.connector'].sudo().create({
            'name': contract.cabal_sequence,
            'responsible_id': user.id,
            'state': 'new',
            'docs_policy': 'in',
            'model': 'sale',
            'sale_id': connector_sale_id,
            'attachment_ids': [(6, 0, [document.id])],
            'monthly_payment': monthly_payment,
            'contract_value': contract_value
        })
        _logger.info("[DocuSign] docusign.connector created: ID=%s, name=%s", 
                    connector_record.id, connector_record.name)
        
        # Create connector line for customer (first signer)
        _logger.info("[DocuSign] Creating docusign.connector.lines for customer: partner_id=%s, email=%s", 
                    contract.partner_id.id, contract.partner_id.email_normalized)
        customer_line = self.env['docusign.connector.lines'].sudo().create({
            'partner_id': contract.partner_id.id,
            'email': contract.partner_id.email_normalized,
            'status': 'draft',
            'un_signed_attachment_ids':  [(6, 0, [document.id])],
            'record_id': connector_record.id,
            'name': document.name
        })
        _logger.info("[DocuSign] Customer connector line created: ID=%s", customer_line.id)
        
        # Create connector line for company signer (second signer)
        _logger.info("[DocuSign] Creating docusign.connector.lines for company signer: user_id=%s, email=%s", 
                    user.id, user.email)
        company_line = self.env['docusign.connector.lines'].sudo().create({
            'partner_id': user.partner_id.id,
            'email': user.email,
            'status': 'draft',
            'un_signed_attachment_ids':  [(6, 0, [document.id])],
            'record_id': connector_record.id,
            'name': document.name
        })
        _logger.info("[DocuSign] Company connector line created: ID=%s", company_line.id)
        
        _logger.info("[DocuSign] Returning connector_record ID=%s with 2 recipients", connector_record.id)
        return connector_record

    def create_print_sign_activity(self):
        for subscription in self:
            activity_type = self.env.ref('mail.mail_activity_data_todo').id
            user_id = subscription.create_uid.id
            summary = 'Print and Sign Contract'
            note = 'Please print and sign the contract.'

            self.env['mail.activity'].sudo().create({
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

    # Redefining methods from the sale_subscription.sale_order.py file to accommodate CPE 

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
        if self.send_method == 'whatsapp':
            phone_raw = contract.partner_id.whatsapp or ''
            match = re.match(r'^\+(\d{1,3})(\d+)$', phone_raw)
            if not match:
                raise ValidationError("The customer does not have a valid WhatsApp number.")
        contract.contract_send_method = self.send_method
        _logger.info("[DocuSign] ContractSendMethodWizard: contract_id=%s, send_method=%s", 
                    contract.id, self.send_method)
        if self.send_method != 'donotsend':
            _logger.info("[DocuSign] Calling action_send_for_signature for contract ID=%s", contract.id)
            return contract.action_send_for_signature()
        else:
            _logger.warning("[DocuSign] Contract NOT SENT - donotsend method selected for contract ID=%s", 
                           contract.id)
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

        # Auto-create installation task and move to schedule state
        try:
            self.subscription_id.action_create_install_task()
        except Exception as e:
            _logger.warning("Failed to auto-create install task: %s", str(e))
            # If task creation fails, still advance state manually
            self.subscription_id.write({'installation_state': 'to_be_scheduled'})

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