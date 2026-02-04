from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools.misc import html_escape
from odoo.tools import float_round
from markupsafe import Markup
from datetime import date, timedelta
import calendar
import hmac
import hashlib
from dateutil.relativedelta import relativedelta
import time, base64, uuid, re, json, jwt, requests
import logging

_logger = logging.getLogger(__name__)


SUBSCRIPTION_DRAFT_STATE = ['1_draft', '2_renewal', '7_upsell']

SUBSCRIPTION_STATES = [
    ('8_suspend', 'Suspended'),  # Suspended
    ('9_transfer', 'Transfer'),  # Transfer
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
    contract_magic_token = fields.Char(string='Contract Magic Token', readonly=True, copy=False)
    contract_magic_link = fields.Char(string='Contract Magic Link', readonly=True, copy=False)
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
    # Extend existing subscription_state instead of redefining it to avoid selection override warnings
    subscription_state = fields.Selection(
        selection_add=SUBSCRIPTION_STATES,
        tracking=True,
        group_expand='_group_expand_states',
    )
    contract_ids = fields.One2many('contract.management', 'subscription_id', string="Contracts")
    contract_count = fields.Integer(string='Contract Count', compute='_compute_contract_count')
    docusign_ids = fields.One2many('docusign.connector', 'sale_id', string="DocuSign Envelopes")
    has_docusign_client_user_id = fields.Boolean(
        string='Has Embedded DocuSign Signer',
        compute='_compute_has_docusign_client_user_id',
        help='True when any related DocuSign connector line has client_user_id set (embedded signing ready).'
    )
    can_resend_contract = fields.Boolean(string='Can Resend Contract', compute='_compute_can_resend_contract')
    payment_change_log_ids = fields.One2many(
        'payment.day.change.log',
        'subscription_id',
        string='Payment Day Changes',
        readonly=True,
        copy=False,
    )
    
    # FSM Integration - Commented out until FSM modules installed in test env
    # fsm_task_ids = fields.One2many('project.task', 'sale_order_id', string="Install Tasks", 
    #                                 domain=[('is_fsm', '=', True)])
    # fsm_task_count = fields.Integer(string='Install Task Count', compute='_compute_fsm_task_count')
    # next_action = fields.Char(string='Next Action', compute='_compute_next_action')
    transfer_date = fields.Date(string="Date of Transfer")
    transfer_reason = fields.Selection(string="Transfer Reason", selection=TRANSFER_REASONS)
    is_transfer = fields.Boolean(string='Is Transfer', default=False, tracking=True)
    previous_partner_id = fields.Many2one('res.partner', string="Previous Client")
    terms_conditions_ids = fields.Many2many('sale.terms.conditions', string='Terms and Conditions')
    cover_letter_id = fields.Many2one('sale.cover.letter', string='Cover Letter', compute='_compute_cover_letter', store=True)
    confirmation_uuid = fields.Char(string='UUID', readonly=True, default=lambda self: str(uuid.uuid4()))
    confirmation_url = fields.Char(string='Confirmation URL', compute='_compute_confirmation_url')
    clause_ids = fields.Many2many('contract.clause', string='Clauses')
    quote_confirmed = fields.Boolean(string='Quote Confirmed', default=False)
    contract_term = fields.Many2one('dte.base.contract', string="Contract Term")
    contract_value = fields.Float(string = "Contract Value")
    last_invoice_date = fields.Date(string='Last Invoice Date', compute='_compute_last_invoice_date', store=False)
    termination_cost = fields.Monetary(
        string='Termination Cost',
        currency_field='currency_id',
        compute='_compute_termination_cost',
        store=False,
    )
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
        store=True,
        help='Determines which stage to show in the progress bar based on subscription state'
    )

    def _get_transfer_display_name(self):
        self.ensure_one()
        sub_name = self.cabal_sequence or self.name or ''
        partner = self.partner_id.display_name or ''
        if sub_name and partner:
            return '%s - %s' % (sub_name, partner)
        return sub_name or partner

    def name_get(self):
        res = super().name_get()
        if not self.env.context.get('contract_transfer_label'):
            return res
        return [(order.id, order._get_transfer_display_name()) for order in self]
    
    @api.depends('confirmation_uuid')
    def _get_confirmation_secret(self):
        """Return HMAC secret for confirmation links; blank means signing is disabled."""
        ICP = self.env['ir.config_parameter'].sudo()
        return ICP.get_param('contract_management.confirm_secret', '')

    @api.depends('contract_ids.docusign_client_user_id')
    def _compute_has_docusign_client_user_id(self):
        for order in self:
            client_ids = order.contract_ids.mapped('docusign_client_user_id') if order.contract_ids else []
            order.has_docusign_client_user_id = any(client_ids)

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

                # Pending contract: contract_state pending_contract
                if contract_state == 'pending_contract':
                    order.progress_stage = 'pending_contract'
                # Pending client signature: contract_state pending_customer_signature
                elif contract_state == 'pending_customer_signature':
                    order.progress_stage = 'pending_client_signature'
                # Pending Cabal signature for identical renewals (no install/config work expected)
                elif (
                    contract_state == 'pending_cabal_signature'
                    and order.renewal_of_id
                    and order.service_change_mode == 'no_change'
                ):
                    order.progress_stage = 'pending_cabal_signature'
                # Speed-only variant renewals show pending Cabal signature while config is handled
                elif (
                    contract_state == 'pending_cabal_signature'
                    and order.renewal_of_id
                    and order.service_change_mode == 'config_only'
                ):
                    order.progress_stage = 'pending_cabal_signature'
                # Identical renewals skip install/activation steps; go straight to active/issue view
                elif order.renewal_of_id and order.service_change_mode == 'no_change':
                    if (
                        contract_state == 'active'
                        and order.installation_state == 'completed'
                        and order.configuration_state == 'completed'
                    ):
                        if order.internet_service_state == 'active':
                            order.progress_stage = 'active'
                        else:
                            order.progress_stage = 'active_with_issues'
                    else:
                        order.progress_stage = 'active_with_issues'
                # Schedule install: installation_state to_be_scheduled
                elif (
                    contract_state == 'pending_cabal_signature'
                    or order.installation_state == 'to_be_scheduled'
                    or order.configuration_state == 'to_be_scheduled'
                ):
                    order.progress_stage = 'schedule_install'
                # Pending install: installation_state scheduled or pending_install
                elif (
                    contract_state == 'pending_cabal_signature'
                    and (
                        order.installation_state == 'scheduled'
                        or order.configuration_state == 'scheduled'
                    )
                ) or order.installation_state =='scheduled' or order.configuration_state == 'scheduled':
                    order.progress_stage = 'pending_install'
                # Active with issues should override contract-sign steps, but only after we surface scheduling states
                elif sub_state == '3_progress' and (
                    contract_state != 'active'
                    or order.installation_state != 'completed'
                    or order.configuration_state != 'completed'
                    or order.internet_service_state != 'active'
                ):
                    order.progress_stage = 'active_with_issues'
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

    def _compute_last_invoice_date(self):
        for order in self:
            invoices = order.invoice_ids.filtered(
                lambda inv: inv.state == 'posted' and getattr(inv, 'move_type', 'out_invoice') == 'out_invoice'
            )
            dates = [inv.invoice_date for inv in invoices if inv.invoice_date]
            order.last_invoice_date = max(dates) if dates else False

    def _compute_termination_cost(self):
        Contract = self.env['contract.management'].sudo()
        for order in self:
            contract = Contract.search([('subscription_id', '=', order.id)], order='create_date desc', limit=1)
            order.termination_cost = contract.early_termination_cost if contract else 0.0

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
            if order._is_speed_only_variant_renewal():
                target_mode = 'config_only'
            else:
                target_mode = 'no_change' if renewal_signature == parent_signature else 'install_no_activation'

            if order.service_change_mode != target_mode:
                order.service_change_mode = target_mode

            # Identical renewals (no_change) should not surface install scheduling; mark install/config done.
            is_renewal = order.subscription_state == '2_renewal' or bool(order.renewal_of_id)
            if is_renewal and order.service_change_mode == 'no_change':
                order.write({
                    'installation_state': 'completed',
                    'configuration_state': 'completed',
                })

            # Speed-only variant renewals should treat install as done and leave config to be scheduled.
            if is_renewal and order.service_change_mode == 'config_only':
                order.write({
                    'installation_state': 'completed',
                    'configuration_state': 'to_be_scheduled',
                })

            # Always carry forward service states from the parent subscription
            if is_renewal and parent:
                state_updates = {
                    'internet_service_state': parent.internet_service_state or 'not_active',
                    'iptv_service_state': parent.iptv_service_state or 'not_active',
                }
                order.write(state_updates)

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

    def _is_identical_renewal(self):
        """True when renewal lines match parent (product + qty)."""
        self.ensure_one()
        if not self.renewal_of_id:
            return False
        parent_signature = self._get_product_signature(self.renewal_of_id)
        renewal_signature = self._get_product_signature(self)
        return parent_signature == renewal_signature

    def _get_product_template_signature(self, order):
        lines = order.order_line.filtered(lambda l: not l.display_type and l.product_id)
        precision = order.env['decimal.precision'].precision_get('Product Unit of Measure') or 6
        return sorted(
            (
                line.product_template_id.id,
                float_round(line.product_uom_qty, precision_digits=precision),
                line.product_uom.id,
            )
            for line in lines
        )

    def _is_speed_only_variant_renewal(self):
        """True when renewal keeps the same base product template(s) but changes variant/speeds."""
        self.ensure_one()
        if not self.renewal_of_id:
            return False
        if self._is_identical_renewal():
            return False

        parent_signature = self._get_product_template_signature(self.renewal_of_id)
        renewal_signature = self._get_product_template_signature(self)
        return parent_signature == renewal_signature

    def _apply_speed_profile_changes(self):
        """Push speed-profile changes to SmartOLT when available."""
        self.ensure_one()

        down_name = self.download_speed_profile_id.name or ''
        up_name = self.upload_speed_profile_id.name or ''
        if not down_name and not up_name:
            return False

        asset = getattr(self, 'cpe_unit_asset', False)
        if not asset:
            self.message_post(body=_("Warning: Speed profile update skipped because no ONU asset is linked."))
            return False

        try:
            self._smartolt_update_location_details(asset)
            resp = self._smartolt_update_speed_profiles(asset, up_name, down_name)
        except Exception as e:
            self.message_post(body=_("Warning: Speed profile update failed: %s") % e)
            return False

        # Record that SmartOLT accepted the change (resp may be None outside production)
        note = _("SmartOLT speed profiles updated (DL: %s / UL: %s).") % (down_name or '-', up_name or '-')
        self.message_post(body=note)
        return True

    def _apply_speed_only_variant_config(self):
        """Finalize config-only renewals that only change speed/variant."""
        self.ensure_one()
        if not self._is_speed_only_variant_renewal():
            return False

        updates = {
            'service_change_mode': 'config_only',
            'installation_state': 'completed',
        }

        success = self._apply_speed_profile_changes()
        if success:
            updates['configuration_state'] = 'completed'
        elif self.configuration_state != 'completed':
            updates['configuration_state'] = 'to_be_scheduled'

        self.with_context(skip_renewal_completion=True).write(updates)
        return success

    def _auto_activate_identical_renewal(self):
        """Skip signature steps for identical renewals and finalize state."""
        self.ensure_one()
        parent = self.renewal_of_id
        if not parent:
            return False

        today = fields.Date.context_today(self)
        cycle_start = parent.next_invoice_date or today
        cycle_end = cycle_start - timedelta(days=1) if cycle_start else today

        updates = {
            'contract_state': 'active',
            'installation_state': 'completed',
            'configuration_state': 'completed',
            'subscription_state': '3_progress',
            'internet_service_state': parent.internet_service_state or 'active',
            'iptv_service_state': parent.iptv_service_state or 'not_active',
            'start_date': cycle_start,
            'next_invoice_date': cycle_start,
            'quote_confirmed': True,
        }
        self.with_context(skip_renewal_completion=True).write(updates)

        if self.state not in ('sale', 'done'):
            self.with_context(skip_renewal_completion=True).action_confirm()

        parent_updates = {
            'subscription_state': '5_renewed',
            'end_date': cycle_end,
        }
        parent.with_context(skip_renewal_completion=True).write(parent_updates)

        self.message_post(
            body=_("Renewal auto-activated (same products/qty as %s).") % (parent.display_name)
        )
        return True

    @api.depends('contract_ids')
    def _compute_contract_count(self):
        for order in self:
            order.contract_count = len(order.contract_ids)

    # subscription_state intentionally left without compute/inverse to allow explicit writes
    
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
        if (
            self.contract_state in ['pending_customer_signature', 'pending_cabal_signature']
            and self.installation_state != 'completed'
        ):
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
    
    def _cm_get_billing_period_delta(self):
        """Return the billing delta for this subscription's plan."""
        self.ensure_one()
        plan = self.plan_id
        value = (getattr(plan, 'billing_period_value', 1) or 1)
        unit = getattr(plan, 'billing_period_unit', 'month') or 'month'

        if unit == 'day':
            return relativedelta(days=value)
        if unit == 'week':
            return relativedelta(days=7 * value)
        if unit == 'year':
            return relativedelta(years=value)
        return relativedelta(months=value)

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

        contract_state_updated_to_pending_cabal = vals.get('contract_state') == 'pending_cabal_signature'
        if contract_state_updated_to_pending_cabal:
            for order in self:
                was_pending_cabal = previous_contract_state.get(order.id) == 'pending_cabal_signature'
                is_now_pending_cabal = order.contract_state == 'pending_cabal_signature'
                if was_pending_cabal or not is_now_pending_cabal:
                    continue

                if order._is_speed_only_variant_renewal():
                    order._apply_speed_only_variant_config()

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
                    if order._is_speed_only_variant_renewal():
                        order._apply_speed_only_variant_config()
                    order._complete_config_changes()

        state_fields_touched = any(key in vals for key in ['contract_state', 'installation_state', 'configuration_state'])
        if state_fields_touched:
            today = fields.Date.context_today(self)
            for order in self:
                is_renewal = bool(order.renewal_of_id) or order.subscription_state == '2_renewal'
                if not is_renewal:
                    continue

                all_active_and_done = (
                    order.contract_state == 'active'
                    and order.installation_state == 'completed'
                    and order.configuration_state == 'completed'
                )
                if not all_active_and_done:
                    continue

                if order.subscription_state == '2_renewal' and order.service_change_mode == 'no_change':
                    order._auto_activate_identical_renewal()
                    continue

                updates = {}
                if order.subscription_state == '2_renewal':
                    updates['subscription_state'] = '3_progress'
                if order.next_invoice_date and order.next_invoice_date < today:
                    updates['next_invoice_date'] = today
                if updates:
                    order.with_context(skip_renewal_completion=True).write(updates)
                if order.state not in ('sale', 'done'):
                    order.with_context(skip_renewal_completion=True).action_confirm()

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
                
    # Method to be used in case a service needs to be transferred
    def action_subscription_transfer_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'subscription.transfer.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_from_subscription_id': self.id,
                'default_transfer_date': fields.Date.context_today(self),
            },
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

    def _prepare_in_person_signing(self, send_method=None):
        """Ensure a DocuSign envelope exists and return the contract record for in-person signing."""
        self.ensure_one()

        chosen_method = send_method or self.contract_send_method or 'email'
        if chosen_method == 'donotsend':
            raise UserError(_("Select a delivery method other than 'Do Not Send' to start in-person signing."))

        # Physical flows do not create envelopes; fall back to email to build an embedded envelope.
        if chosen_method == 'physical':
            chosen_method = 'email'

        if self.contract_send_method != chosen_method:
            self.sudo().write({'contract_send_method': chosen_method})

        contract_record = False
        if self.contract_ids:
            contract_record = self.contract_ids.sorted(lambda r: r.id)[-1]

        # If there is no envelope yet, send the contract now using the selected method.
        if not contract_record or not contract_record.docusign_id:
            self.action_send_for_signature()
            if self.contract_ids:
                contract_record = self.contract_ids.sorted(lambda r: r.id)[-1]

        if not contract_record or not contract_record.docusign_id:
            raise ValidationError(_("Could not prepare a DocuSign envelope for in-person signing."))

        return contract_record

    def action_sign_in_person(self):
        self.ensure_one()

        contract_record = self._prepare_in_person_signing(self.contract_send_method)
        return {
            'type': 'ir.actions.act_url',
            'url': f"/contracts/sign/in_person/{contract_record.id}",
            'target': 'self',
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
        """Send quote via WhatsApp template when possible; fallback to email."""
        self.ensure_one()

        ICP = self.env['ir.config_parameter'].sudo()

        base_url = ICP.get_param('web.base.url', '')
        logo_url = (ICP.get_param('wa_logo_file', '') or '').strip()

        _logger.info(
            "[QuoteSend] action_quotation_send order=%s id=%s base_url=%s",
            self.name,
            self.id,
            base_url,
        )

        partner_for_comm = self.partner_id.commercial_partner_id or self.partner_id
        prefers_whatsapp = bool(getattr(partner_for_comm, 'preference_wa', False))
        whatsapp_number = partner_for_comm.whatsapp or ''
        can_use_whatsapp = prefers_whatsapp and bool(whatsapp_number)

        # Temporary kill-switch: force quotations to be sent by email only
        force_email_only = ICP.get_param('contract_management.force_quote_email_only', '1')
        force_email_only = (force_email_only or '').strip().lower() in ('1', 'true', 'yes', 'on')
        if force_email_only and can_use_whatsapp:
            _logger.info("[QuoteSend] WhatsApp sending disabled via config; using email for order %s", self.name)
            can_use_whatsapp = False

        pdf_base64 = None
        if can_use_whatsapp:
            action_report = self.env.ref('sale.action_report_saleorder', raise_if_not_found=False)
            if not action_report:
                _logger.warning(
                    "[QuoteSend] Missing report action sale.action_report_saleorder; skipping WhatsApp send for %s",
                    self.name,
                )
                can_use_whatsapp = False
            else:
                try:
                    pdf_content, _report_format = action_report._render_qweb_pdf(
                        report_ref='sale.action_report_saleorder',
                        res_ids=self.ids,
                    )
                    pdf_base64 = base64.b64encode(pdf_content).decode('utf-8') if pdf_content else None
                    if not pdf_base64:
                        _logger.warning(
                            "[QuoteSend] Empty PDF content for order %s; skipping WhatsApp send",
                            self.name,
                        )
                        can_use_whatsapp = False
                except Exception as exc:
                    _logger.warning(
                        "[QuoteSend] Failed to render PDF for order %s; skipping WhatsApp send: %s",
                        self.name,
                        exc,
                        exc_info=True,
                    )
                    can_use_whatsapp = False

        if can_use_whatsapp:
            _logger.info(
                "[QuoteSend] Partner %s prefers WhatsApp and has number %s",
                partner_for_comm.name,
                whatsapp_number,
            )
            confirmation_link = self.confirmation_url or self.get_portal_url()
            WhatsApp = self.env['whatsapp.comm']

            normalized_phone = WhatsApp.normalize_phone(whatsapp_number)
            client_phone = f"+{normalized_phone}" if normalized_phone else None

            if client_phone:
                context_info = f"quote {self.id} to {partner_for_comm.name} ({client_phone})"
                recipient_phone, test_mode = WhatsApp._apply_test_mode_phone(client_phone, context_info)
                _logger.info(
                    "[QuoteSend] Preparing WhatsApp template 'quote_confirmation' for order %s to phone %s (test_mode=%s)",
                    self.name,
                    recipient_phone,
                    test_mode,
                )

                safe_link = confirmation_link or ''
                button_param = safe_link
                if 'confirm_sale_order' in safe_link:
                    button_param = safe_link.split('confirm_sale_order', 1)[1]
                if not button_param:
                    button_param = base_url or '/'

                pdf_media_url = None
                try:
                    if pdf_base64:
                        attachment = self.env['ir.attachment'].sudo().create({
                            'name': f"{self.name}.pdf",
                            'datas': pdf_base64,
                            'type': 'binary',
                            'res_model': self._name,
                            'res_id': self.id,
                            'mimetype': 'application/pdf',
                        })

                        token = attachment.access_token or attachment.generate_access_token()
                        if isinstance(token, (list, tuple, set)):
                            token = next(iter(token), '')
                        if token and not isinstance(token, str):
                            token = str(token)
                        try:
                            attachment.sudo().write({'public': True})
                        except Exception:
                            _logger.debug(
                                "[QuoteSend] Unable to mark attachment %s public for order %s",
                                attachment.id,
                                self.name,
                                exc_info=True,
                            )

                        filename_param = attachment.name or 'quotation.pdf'
                        if not filename_param.lower().endswith('.pdf'):
                            filename_param += '.pdf'
                        filename_param = filename_param.replace(' ', '_')

                        pdf_media_url = f"{attachment.id}/{filename_param}?download=1"
                        if token:
                            pdf_media_url += f"&access_token={token}"
                except Exception:
                    _logger.warning(
                        "[QuoteSend] Failed to create attachment for WhatsApp header on order %s",
                        self.name,
                        exc_info=True,
                    )

                if not pdf_media_url:
                    _logger.warning(
                        "[QuoteSend] No media_url available for order %s; skipping WhatsApp send",
                        self.name,
                    )
                    can_use_whatsapp = False

                if can_use_whatsapp and not logo_url:
                    _logger.warning(
                        "[QuoteSend] System parameter wa_logo_file is empty; skipping WhatsApp send for %s",
                        self.name,
                    )
                    can_use_whatsapp = False

                if can_use_whatsapp:
                    rich_template_data = {
                        "header": {
                            "type": "image",
                            "media_url": logo_url,
                        },
                        "body": {
                            "params": [
                                {"data": partner_for_comm.name or ''},
                            ]
                        },
                        "button": {
                            "subType": "url",
                            "params": [
                                {"data": pdf_media_url or ''},
                                {"data": button_param},
                            ],
                        }
                    }

                if can_use_whatsapp:
                    partner_lang = partner_for_comm.lang or 'es'
                    lang_code = (partner_lang or 'es')[:2]

                    payload = WhatsApp._build_fc_payload(
                        to_phone=recipient_phone,
                        template_name='confirm_quotation',
                        language_code=lang_code,
                        rich_template_data=rich_template_data,
                    )

                    try:
                        result = WhatsApp._send_fc_template_request(payload)

                        if not result.get('success'):
                            error_msg = result.get('error') or _('Unknown error sending WhatsApp')
                            raise UserError(error_msg)

                        response = result.get('response') or {}

                        whatsapp_comm = None

                        if response.get('request_id'):
                            base_vals = {
                                "name": _("Cotizacion"),
                                "partner_id": partner_for_comm.id,
                                "sale_order": self.id,
                                "to_phone": recipient_phone,
                                "template_name": 'confirm_quotation',
                            }

                            whatsapp_comm = WhatsApp._create_fc_whatsapp_log(
                                base_vals=base_vals,
                                response_dict=result,
                                verification_dict=None,
                                test_mode=test_mode,
                            )

                        if self.state in ('draft', 'sent'):
                            self.write({'state': 'sent'})

                        status_link = None
                        if whatsapp_comm:
                            status_link = f"/web#id={whatsapp_comm.id}&model=whatsapp.comm&view_type=form"

                        body_message = _('Quotation sent automatically via WhatsApp to %s.') % whatsapp_number
                        if status_link:
                            body_message = Markup("%s <a href=\"%s\">%s</a>") % (
                                html_escape(body_message),
                                html_escape(status_link),
                                html_escape(_('View send status')),
                            )

                        self.message_post(
                            body=body_message,
                            subtype_xmlid='mail.mt_note',
                            message_type='comment',
                        )

                        _logger.info("[QuoteSend] WhatsApp template queued successfully for order %s", self.name)

                        return {
                            'type': 'ir.actions.client',
                            'tag': 'display_notification',
                            'params': {
                                'title': _('Quotation Queued'),
                                'message': _('Quotation will be sent via WhatsApp to %s shortly.') % whatsapp_number,
                                'type': 'success',
                                'sticky': False,
                            }
                        }

                    except Exception as exc:
                        _logger.warning(
                            "[QuoteSend] WhatsApp send failed for order %s, falling back to email: %s",
                            self.name,
                            exc,
                            exc_info=True,
                        )

        if not self.partner_id.email:
            _logger.warning(
                "[QuoteSend] Order %s: Customer %s has no email address",
                self.name,
                self.partner_id.name,
            )
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

        template = self.env.ref('sale.email_template_edi_sale', raise_if_not_found=False)
        if not template:
            _logger.error("[QuoteSend] Email template 'sale.email_template_edi_sale' not found")
            raise UserError(_('Quotation email template not found. Please contact administrator.'))

        _logger.info(
            "[QuoteSend] Queueing email using template ID=%s to customer %s (%s)",
            template.id,
            self.partner_id.name,
            self.partner_id.email,
        )

        try:
            template.send_mail(self.id, force_send=False, raise_exception=False)

            if self.state in ('draft', 'sent'):
                self.write({'state': 'sent'})

            template_name = template.display_name or template.name or _('quotation template')
            self.message_post(
                body=_('Quotation sent automatically to %s using template %s.') % (
                    self.partner_id.email,
                    template_name,
                ),
                subtype_xmlid='mail.mt_note',
                message_type='comment',
            )

            _logger.info("[QuoteSend] Email queued successfully for order %s", self.name)

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

            # Always send contracts for renewals; do not auto-activate identical renewals
            
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
            
            # Step 0: Choose delivery method with WhatsApp-first, email fallback
            send_method = contract.contract_send_method or 'whatsapp'
            partner_email = (contract.partner_id.email or '').strip()
            email_domain = partner_email.split('@')[-1].lower() if '@' in partner_email else ''
            has_whatsapp = bool(contract.partner_id.whatsapp)

            if send_method == 'whatsapp' and not has_whatsapp:
                send_method = 'email'
                contract.sudo().write({'contract_send_method': send_method})
                _logger.info(
                    "[DocuSign] Partner has no WhatsApp; falling back to email for contract ID=%s",
                    contract.id,
                )

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
            
            # Step 4: Create (or reuse) the connector and connector line records (if not physical)
            connector_id = None
            if send_method != 'physical':
                # Reuse the most recent connector with no signatures to avoid duplicate envelopes on resend
                base_order_for_connector = contract._get_addendum_base_order()
                connector_pool = contract.docusign_ids
                if base_order_for_connector and base_order_for_connector != contract:
                    connector_pool |= base_order_for_connector.docusign_ids

                reusable_connector = connector_pool.filtered(
                    lambda c: c.connector_line_ids
                    and not any(line.sign_status for line in c.connector_line_ids)
                    and c.state in ('new', 'sent', 'draft')
                )

                if reusable_connector:
                    connector_id = reusable_connector.sorted(key=lambda c: c.id, reverse=True)[0]
                    _logger.info(
                        "[DocuSign] Reusing existing DocuSign connector ID=%s for contract ID=%s",
                        connector_id.id,
                        contract.id,
                    )
                    connector_id.sudo().write({
                        'attachment_ids': [(6, 0, [document.id])],
                        'monthly_payment': monthly_payment,
                        'contract_value': contract_value,
                    })
                else:
                    _logger.info("[DocuSign] Creating DocuSign connector for contract ID=%s", contract.id)
                    connector_id = self._send_document_to_docusign(contract, document)
                    _logger.info(
                        "[DocuSign] DocuSign connector created: ID=%s, name=%s",
                        connector_id.id,
                        connector_id.name,
                    )

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
            if send_method != 'physical' and connector_id:
                k_management.sudo().write({'docusign_id': connector_id.id})
                _logger.info("[DocuSign] contract.management updated with docusign_id=%s", connector_id.id)
            
            # Step 7: Send document (after local records are created)
            base_order = contract._get_addendum_base_order()
            target_order = base_order if base_order and base_order != contract else contract

            if send_method != 'physical':
                # Send document from Docusign
                _logger.info("[DocuSign] Calling send_docs() with send_method=%s for connector ID=%s", 
                            send_method, connector_id.id)
                send_contract_result = connector_id.send_docs(send_method)
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

    def action_open_resend_contract_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'contract.resend.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_contract_id': self.id,
                'active_id': self.id,
            },
        }

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

        # Generate a fresh magic link for embedded signing (no login required)
        token, magic_url = customer_line.generate_magic_link()
        link_msg = _("Magic signing link (no login): %s") % magic_url
        connector_record.message_post(body=link_msg)
        contract_record = self.env['contract.management'].sudo().search([
            ('subscription_id', '=', contract.id)
        ], limit=1)
        if contract_record:
            contract_record.message_post(body=link_msg)

        if contract.contract_send_method == 'whatsapp':
            try:
                contract._send_magic_link_via_whatsapp(contract.partner_id, token, magic_url)
            except Exception as exc:
                _logger.error("[DocuSign] Failed to send magic link via WhatsApp: %s", exc, exc_info=True)
                raise
        
        _logger.info("[DocuSign] Returning connector_record ID=%s with 2 recipients", connector_record.id)
        return connector_record

    def _compute_docusign_recipient_email(self, partner):
        """Return a usable recipient email, synthesizing one when the partner has none."""
        self.ensure_one()
        if partner.email_normalized:
            return partner.email_normalized
        if partner.email:
            return partner.email

        placeholder_domain = (
            self.env['ir.config_parameter']
            .sudo()
            .get_param('contract_management.docusign_placeholder_email_domain')
            or 'signing.cabalinternal.local'
        )
        phone = partner.mobile or partner.whatsapp or partner.phone or ''
        phone_digits = re.sub(r'\D', '', phone) if phone else 'noemail'
        return f"contract-{self.id or partner.id}-{phone_digits}@{placeholder_domain}"

    def _send_magic_link_via_whatsapp(self, partner, token, magic_url=None):
        self.ensure_one()

        WhatsApp = self.env['whatsapp.comm']
        ICP = self.env['ir.config_parameter'].sudo()
        logo = ICP.get_param('wa_logo_file', '')
        template_name = ICP.get_param('contract_management.wa_magic_template', 'firmar_contrato')

        base_url = (ICP.get_param('web.base.url', '') or '').rstrip('/')
        if not magic_url:
            path = f"/contracts/sign/{token}"
            magic_url = f"{base_url}{path}" if base_url else path

        # Prefer explicit WhatsApp number, then fall back to mobile/phone
        normalized_phone = WhatsApp.normalize_phone(partner.whatsapp)
        client_phone = f'+{normalized_phone}' if normalized_phone else None
        if not client_phone:
            raise ValidationError(_("Número de teléfono inválido"))
        
        context_info = f"magic link contract {self.id} to {partner.name} ({client_phone})"
        recipient_phone, test_mode = WhatsApp._apply_test_mode_phone(client_phone, context_info)

        partner_lang = partner.lang or ICP.get_param('wa_template_language', 'es_ES')
        lang_code = (partner_lang or 'es').split('_')[0]

        rich_template_data = {
            "body": {
                "params": [
                    {"data": partner.name or ''},
                    {"data": magic_url},
                ]
            }
        }

        if logo:
            rich_template_data["header"] = {"type": "image", "media_url": str(logo)}

        payload = WhatsApp._build_fc_payload(
            to_phone=recipient_phone,
            template_name=template_name,
            language_code=lang_code,
            rich_template_data=rich_template_data,
        )

        result = WhatsApp._send_fc_template_request(payload)
        
        if not result.get('success'):
            error_msg = result.get('error') or _('Unknown error sending WhatsApp')
            _logger.error("[DocuSign] WhatsApp magic link send failed: %s", error_msg)
            raise ValidationError(_("No se pudo enviar el enlace por WhatsApp: %s") % error_msg)

        response = result.get('response') or {}
        log_record = False

        if response.get('request_id'):
            base_vals = {
                "name": _("Enlace de firma"),
                "partner_id": partner.id,
                "sale_order": self.id,
                "to_phone": recipient_phone,
                "template_name": template_name,
            }

            log_record = WhatsApp._create_fc_whatsapp_log(
                base_vals=base_vals,
                response_dict=result,
                verification_dict=None,
                test_mode=test_mode,
            )
        request_id = response.get('request_id') or (log_record.request_id if log_record else '')
        message = _("Enlace de firma enviado por WhatsApp")

        if test_mode:
            message += " [TEST MODE]"
        if request_id:
            message += _(" (Request ID: %s)") % request_id

        self.message_post(body=message)

        return log_record or True

    def action_send_contract_link_whatsapp(self):
        """Send the existing contract magic link via WhatsApp template."""
        self.ensure_one()

        connector = self.docusign_ids.filtered(lambda c: c.connector_line_ids)
        connector = connector.sorted(key=lambda c: c.id, reverse=True)[:1]

        if not connector:
            raise ValidationError(_("No DocuSign envelope is available to build a magic link. Send the contract first."))

        customer_line = connector.connector_line_ids.filtered(lambda l: l.partner_id.id == self.partner_id.id)[:1]
        if not customer_line:
            customer_line = connector.connector_line_ids[:1]

        if not customer_line:
            raise ValidationError(_("No DocuSign recipient was found to generate a magic link."))

        token, magic_url = customer_line.generate_magic_link()

        return self._send_magic_link_via_whatsapp(self.partner_id, token, magic_url)

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

    def action_open_change_payment_day_wizard(self):
        self.ensure_one()
        payment_day = (self.next_invoice_date or fields.Date.context_today(self)).day

        view = self.env.ref('contract_management.view_change_payment_date_wizard')

        return {
            'type': 'ir.actions.act_window',
            'name': _('Change Payment Day'),
            'res_model': 'change.payment.date.wizard',
            'view_mode': 'form',
            'views': [(view.id, 'form')],
            'target': 'new',
            'view_id': view.id,
            'context': {
                **self.env.context,
                'default_subscription_id': self.id,
                'default_payment_day': payment_day,
                'default_wizard_step': 'select',
                'default_show_advanced': False,
            },
        }

    def action_open_change_payment_day_batch_wizard(self):
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id or self.partner_id
        default_day = (self.next_invoice_date or fields.Date.context_today(self)).day
        return {
            'type': 'ir.actions.act_window',
            'name': _('Align Payment Day (All Subs)'),
            'res_model': 'change.payment.date.batch.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_partner_id': partner.id,
                'default_payment_day': default_day,
            },
        }
    def send_quote_via_whatsapp(self, records):

        auth_token = self.env['ir.config_parameter'].get_param('fc_auth_token', '')
        fc_url_base = self.env['ir.config_parameter'].get_param('fc_url_base', '')
        fc_url_send = self.env['ir.config_parameter'].get_param('fc_url_send', '')
        namespace = self.env['ir.config_parameter'].get_param('wa_namespace', '')
        message_template = self.env['ir.config_parameter'].get_param('wa_template_quote', 'confirmacion_de_orden')

        WhatsApp = self.env['whatsapp.comm']

        responses = []
        for rec in records:
            if not rec.partner_id.whatsapp:
                raise ValidationError("Cliente no tiene numero de WhatsApp registrado")

            normalized_phone = WhatsApp.normalize_phone(rec.partner_id.whatsapp)
            if not normalized_phone:
                raise ValidationError("Número de teléfono inválido")

            client_phone = f"+{normalized_phone}"
            context_info = f"quote {rec.id} to {rec.partner_id.name} ({client_phone})"
            recipient_phone, _ = WhatsApp._apply_test_mode_phone(client_phone, context_info)

            # Render the quote PDF so we can attach it as a document header.
            pdf_content, _ = self.env.ref('sale.action_report_saleorder')._render_qweb_pdf(rec.id)
            pdf_base64 = base64.b64encode(pdf_content).decode('utf-8')

            headers = {
                'Content-Type': 'application/json; charset=utf-8',
                'Accept': 'application/json',
                'Authorization': auth_token,
            }

            payload = {
                "from": {"phone_number": "+50379401214"},
                "provider": "whatsapp",
                "to": [{"phone_number": recipient_phone}],
                "data": {
                    "message_template": {
                        "storage": "conversation",
                        "template_name": message_template,
                        "namespace": namespace,
                        "language": {"policy": "deterministic", "code": "es"},
                        "rich_template_data": {
                            "header": {
                                "type": "document",
                                "document": {
                                    "link": f"data:application/pdf;base64,{pdf_base64}",
                                    "filename": f"{rec.name}.pdf",
                                },
                            },
                            "body": {"params": [{"data": rec.partner_id.name}]},
                            "button": {
                                "sub_type": "url",
                                "index": "0",
                                "parameters": [
                                    {"type": "text", "text": f"{rec.confirmation_url}&send_method=whatsapp"}
                                ],
                            },
                        },
                    }
                },
            }

            wa_sent = requests.post(f"{fc_url_base}/{fc_url_send}", headers=headers, json=payload, timeout=30)
            response = wa_sent.json()
            responses.append(response)

            if 'request_id' in response:
                rec.message_post(body="Notificacion por WhatsApp enviada con Request ID " + str(response['request_id']))
            else:
                rec.message_post(body="Notificacion por WhatsApp FALLIDA con codigo " + str(wa_sent.status_code))
                self.env['helpdesk.ticket'].sudo().create({
                    'name': "Unable to Send WhatsApp",
                    'description': "WhatsApp Notification to " + str(client_phone) + " was " + str(response),
                    'message_needaction': True,
                    'ticket_type_id': 4,
                })

        return responses

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

class ContractResendWizard(models.TransientModel):
    _name = 'contract.resend.wizard'
    _description = 'Contract Resend Wizard'

    contract_id = fields.Many2one(
        'sale.order',
        string='Contract',
        required=True,
        default=lambda self: self.env.context.get('default_contract_id') or self.env.context.get('active_id'),
    )
    can_sign_in_person = fields.Boolean(compute='_compute_capabilities')
    can_send_magic_link = fields.Boolean(compute='_compute_capabilities')
    can_resend_email = fields.Boolean(compute='_compute_capabilities')
    can_open_portal = fields.Boolean(compute='_compute_capabilities')

    @api.depends('contract_id')
    def _compute_capabilities(self):
        for wizard in self:
            contract = wizard.contract_id
            wizard.can_resend_email = bool(contract and contract.can_resend_contract)
            wizard.can_sign_in_person = bool(
                contract
                and contract.progress_stage == 'pending_client_signature'
                and contract.has_docusign_client_user_id
            )
            wizard.can_send_magic_link = wizard.can_sign_in_person
            wizard.can_open_portal = wizard.can_send_magic_link

    def _get_contract(self):
        self.ensure_one()
        contract = self.contract_id or self.env['sale.order'].browse(self.env.context.get('active_id'))
        if not contract:
            raise UserError(_("No active contract found."))
        return contract

    def action_resend_email(self):
        contract = self._get_contract()
        if not self.can_resend_email:
            raise ValidationError(_("Contract cannot be resent right now."))
        result = contract.action_resend_contract()
        return result or {'type': 'ir.actions.act_window_close'}

    def action_send_magic_link(self):
        contract = self._get_contract()
        if not self.can_send_magic_link:
            raise ValidationError(_("Contract is not ready for a magic link."))
        contract.action_send_contract_link_whatsapp()
        return {'type': 'ir.actions.act_window_close'}

    def action_sign_in_person(self):
        contract = self._get_contract()
        if not self.can_sign_in_person:
            raise ValidationError(_("Contract is not ready for in-person signing."))
        return contract.action_sign_in_person()

    def action_open_portal(self):
        contract = self._get_contract()
        if not self.can_open_portal:
            raise ValidationError(_("Contract is not ready for portal signing."))
        contract._portal_ensure_token()
        url = contract.get_portal_url()
        return {
            'type': 'ir.actions.act_url',
            'url': url,
            'target': 'self',
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

    def action_sign_in_person(self):
        self.ensure_one()
        contract_id = self.env.context.get('active_id')
        if not contract_id:
            raise UserError("No active contract found.")

        contract = self.env['sale.order'].browse(contract_id)
        contract_record = contract._prepare_in_person_signing(self.send_method)

        return {
            'type': 'ir.actions.act_url',
            'url': f"/contracts/sign/in_person/{contract_record.id}",
            'target': 'self',
        }

class SubscriptionTransferWizard(models.TransientModel):
    _name = 'subscription.transfer.wizard'
    _description = 'Subscription Transfer Wizard'

    state = fields.Selection(
        [('select', 'Select Subscriptions'), ('confirm', 'Confirm Transfer')],
        default='select',
        required=True,
    )
    from_subscription_id = fields.Many2one('sale.order', string='From Subscription', required=True)
    to_subscription_id = fields.Many2one(
        'sale.order',
        string='To Subscription',
        required=True,
        domain="[('contract_state', '=', 'active'), ('id', '!=', from_subscription_id)]",
    )
    transfer_date = fields.Date(
        string='Effective Date of Transfer',
        default=fields.Date.context_today,
        required=True,
    )
    from_summary = fields.Html(string='From Summary', compute='_compute_summaries', sanitize=False)
    to_summary = fields.Html(string='To Summary', compute='_compute_summaries', sanitize=False)
    from_label = fields.Char(string='From Label', compute='_compute_labels')
    to_label = fields.Char(string='To Label', compute='_compute_labels')
    confirm_ack = fields.Boolean(string='I confirm the transfer details are correct')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ctx = self.env.context or {}
        from_id = (
            ctx.get('default_from_subscription_id')
            or ctx.get('active_id')
            or (ctx.get('active_ids') and ctx['active_ids'][0])
        )
        if from_id:
            res.setdefault('from_subscription_id', from_id)
        return res

    def _build_label(self, subscription):
        if not subscription:
            return ''

        sub_name = subscription.cabal_sequence or subscription.name or ''
        partner = subscription.partner_id.display_name or ''
        if sub_name and partner:
            return '%s - %s' % (sub_name, partner)
        return sub_name or partner

    def _build_summary(self, subscription):
        if not subscription:
            return ''

        parts = []
        parts.append(_('Partner: %s') % html_escape(subscription.partner_id.display_name or ''))
        if subscription.partner_shipping_id:
            parts.append(_('Service Address: %s') % html_escape(subscription.partner_shipping_id.contact_address or ''))
        parts.append(_('Contract: %s') % html_escape(subscription.cabal_sequence or subscription.name or ''))
        if subscription.start_date:
            parts.append(_('Start: %s') % subscription.start_date)
        if subscription.end_date:
            parts.append(_('End: %s') % subscription.end_date)

        # Basic service/equipment hints (shown only if fields exist)
        equipment = []
        for fname, label in [
            ('cpe_unit', _('ONT/Router')),
            ('cpe_stb', _('STB')),
            ('download_speed_profile_id', _('Download Profile')),
            ('upload_speed_profile_id', _('Upload Profile')),
        ]:
            if fname in subscription._fields:
                val = subscription[fname]
                if val:
                    equipment.append('%s: %s' % (label, html_escape(val.display_name if hasattr(val, 'display_name') else str(val))))

        if equipment:
            parts.append(_('Equipment/Config: %s') % ', '.join(equipment))

        return Markup('<br/>').join(parts)

    @api.depends('from_subscription_id', 'to_subscription_id', 'transfer_date')
    def _compute_summaries(self):
        for wiz in self:
            wiz.from_summary = wiz._build_summary(wiz.from_subscription_id)
            wiz.to_summary = wiz._build_summary(wiz.to_subscription_id)

    @api.depends('from_subscription_id', 'to_subscription_id')
    def _compute_labels(self):
        for wiz in self:
            wiz.from_label = wiz._build_label(wiz.from_subscription_id)
            wiz.to_label = wiz._build_label(wiz.to_subscription_id)

    def action_review(self):
        self.ensure_one()
        # Validate selections before showing confirmation
        if not self.from_subscription_id or not self.to_subscription_id:
            raise ValidationError(_('Select both the source and destination subscriptions before reviewing.'))
        if self.from_subscription_id == self.to_subscription_id:
            raise ValidationError(_('The source and destination subscriptions must be different.'))

        self.write({'state': 'confirm'})
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'subscription.transfer.wizard',
            'view_mode': 'form',
            'res_id': self.id,
            'target': 'new',
        }

    def _validate_destination_contract(self, to_subscription):
        has_active_contract = (
            to_subscription.contract_state == 'active'
            or bool(to_subscription.contract_ids.filtered(lambda c: c.state == 'active'))
        )
        if not has_active_contract:
            raise ValidationError(_('The destination subscription must have an active contract before transferring service.'))

    def transfer_subscription(self):
        self.ensure_one()
        from_sub = self.from_subscription_id
        to_sub = self.to_subscription_id
        from_fields = from_sub._fields
        to_fields = to_sub._fields

        if self.state != 'confirm' or not self.confirm_ack:
            raise ValidationError(_('Please review and confirm the transfer details before proceeding.'))

        def _field(record, name):
            return record[name] if name in record._fields else False

        def _filter_field_vals(record, vals):
            return {key: value for key, value in vals.items() if key in record._fields}

        if not from_sub or not to_sub:
            raise ValidationError(_('Select both the source and destination subscriptions.'))
        if from_sub == to_sub:
            raise ValidationError(_('The source and destination subscriptions must be different.'))

        self._validate_destination_contract(to_sub)

        # Require the destination contract to end no earlier than the source contract
        from_end_date = from_sub.end_date
        to_end_date = to_sub.end_date
        if from_end_date:
            if not to_end_date or to_end_date < from_end_date:
                raise ValidationError(
                    _('The destination contract must end on or after the source contract (%s). Please extend the destination contract before transferring.')
                    % from_end_date
                )

        if any([
            _field(to_sub, 'cpe_unit'),
            _field(to_sub, 'cpe_unit_asset'),
            _field(to_sub, 'cpe_stb'),
            _field(to_sub, 'cpe_stb_asset'),
        ]):
            raise ValidationError(_('The destination subscription already has equipment assigned. Clear it before transferring.'))

        transfer_date = self.transfer_date or fields.Date.context_today(self)

        # Capture data before clearing the source subscription
        cpe_unit = _field(from_sub, 'cpe_unit')
        cpe_unit_asset = _field(from_sub, 'cpe_unit_asset')
        cpe_stb = _field(from_sub, 'cpe_stb')
        cpe_stb_asset = _field(from_sub, 'cpe_stb_asset')
        iptv_account = _field(from_sub, 'iptv_account')
        ip_address = _field(from_sub, 'ip_address')
        is_shared_connection = _field(from_sub, 'is_shared_connection')
        is_wireless_connection = _field(from_sub, 'is_wireless_connection')

        # Clear source subscription and close it
        from_sub_vals = {
            'cpe_unit': False,
            'cpe_unit_asset': False,
            'cpe_stb': False,
            'cpe_stb_asset': False,
            'iptv_account': False,
            'ip_address': False,
            'is_shared_connection': False,
            'is_wireless_connection': False,
            'download_speed_profile_id': False,
            'upload_speed_profile_id': False,
            'end_date': transfer_date,
            'subscription_state': '6_churn',
        }
        from_sub.write(_filter_field_vals(from_sub, from_sub_vals))

        # Assign captured values to the destination subscription
        to_sub_vals = {
            'cpe_unit': cpe_unit.id if cpe_unit else False,
            'cpe_unit_asset': cpe_unit_asset.id if cpe_unit_asset else False,
            'cpe_stb': cpe_stb.id if cpe_stb else False,
            'cpe_stb_asset': cpe_stb_asset.id if cpe_stb_asset else False,
            'iptv_account': iptv_account,
            'ip_address': ip_address,
            'is_shared_connection': is_shared_connection,
            'is_wireless_connection': is_wireless_connection,
            'origin_order_id': from_sub.id,
            'is_transfer': True,
            'start_date': fields.Date.context_today(self),
            'next_invoice_date': fields.Date.context_today(self),
            'installation_state': 'completed',
            'configuration_state': 'completed',
        }
        if cpe_unit or cpe_unit_asset:
            to_sub_vals['internet_service_state'] = 'active'
        if cpe_stb or cpe_stb_asset or iptv_account:
            to_sub_vals['iptv_service_state'] = 'active'
        to_sub.write(_filter_field_vals(to_sub, to_sub_vals))

        # Confirm destination subscription and invoice immediately after transfer
        invoice = False
        try:
            to_sub.action_confirm()
            invoice = to_sub._create_invoices()
            if invoice:
                invoice.action_post()
        except Exception as e:
            _logger.warning("[Transfer] Confirm/invoice failed for destination subscription %s: %s", to_sub.id, e)
            to_sub.message_post(body=_("Invoice on transfer failed: %s") % e)

        # Prepare cross-links for chatter/context
        link_to_sub = Markup("<a href=\"/web#id=%s&model=sale.order&view_type=form\">%s</a>" % (to_sub.id, html_escape(to_sub.display_name)))
        link_from_sub = Markup("<a href=\"/web#id=%s&model=sale.order&view_type=form\">%s</a>" % (from_sub.id, html_escape(from_sub.display_name)))

        # Terminate contracts linked to the source subscription and log why
        contracts_to_terminate = from_sub.contract_ids.filtered(lambda c: c.state in ('active', 'renewal_due', 'expired'))
        for contract in contracts_to_terminate:
            contract.write({'state': 'terminated'})
            contract.message_post(body=Markup(_('Contract terminated due to service transfer to %(dest)s on %(date)s.')) % {
                'dest': link_to_sub,
                'date': transfer_date,
            })

        # Reassign assets to the destination subscription and partner
        if cpe_unit_asset and 'subscription_id' in cpe_unit_asset._fields:
            cpe_unit_asset.sudo().write({
                'partner_id': to_sub.partner_id.id,
                'subscription_id': to_sub.id,
                'client_name': to_sub.partner_id.name,
            })
        if cpe_stb_asset and 'subscription_id' in cpe_stb_asset._fields:
            cpe_stb_asset.sudo().write({
                'partner_id': to_sub.partner_id.id,
                'subscription_id': to_sub.id,
                'client_name': to_sub.partner_id.name,
            })

        # Update SmartOLT context (location + speeds) after transfer
        try:
            if hasattr(to_sub, "smartolt_push_transfer_update"):
                result = to_sub.smartolt_push_transfer_update(from_sub) or {}
                note_lines = result.get('notes') or []
                if note_lines:
                    msg = "\n".join(note_lines)
                    to_sub.message_post(body=_("SmartOLT transfer update:\n%s") % msg)
        except Exception as e:
            _logger.warning(
                "SmartOLT update after transfer failed (from %s to %s): %s",
                from_sub.id,
                to_sub.id,
                e,
            )
            to_sub.message_post(body=_("SmartOLT update after transfer failed: %s") % e)

        _logger.info(
            "[Transfer] Auto-invoicing skipped during subscription transfer (from %s to %s)",
            from_sub.id,
            to_sub.id,
        )

        # Log chatter notes on both subscriptions with cross-links
        from_sub.message_post(body=Markup(_('Service transferred to %(dest)s on %(date)s.')) % {
            'dest': link_to_sub,
            'date': transfer_date,
        })
        to_sub.message_post(body=Markup(_('Service transferred from %(src)s on %(date)s.')) % {
            'src': link_from_sub,
            'date': transfer_date,
        })

        return {'type': 'ir.actions.act_window_close'}
    
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



class PaymentDayChangeLog(models.Model):
    _name = 'payment.day.change.log'
    _description = 'Payment Day Change Log'
    _order = 'change_date desc'

    subscription_id = fields.Many2one('sale.order', string='Subscription', required=True, ondelete='cascade')
    change_date = fields.Datetime(string='Changed On', default=fields.Datetime.now, readonly=True)
    changed_by_id = fields.Many2one('res.users', string='Changed By', default=lambda self: self.env.user, readonly=True)
    previous_next_invoice_date = fields.Date(string='Previous Next Invoice Date', readonly=True)
    new_next_invoice_date = fields.Date(string='New Next Invoice Date', readonly=True)
    prorated_amount = fields.Monetary(string='Prorated Amount', readonly=True, currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', string='Currency', required=True)


class ChangePaymentDateWizard(models.TransientModel):
    _name = 'change.payment.date.wizard'
    _description = 'Change Payment Date Wizard'

    subscription_id = fields.Many2one(
        'sale.order',
        string='Subscription',
        required=True,
        default=lambda self: self._default_subscription_id(),
    )
    wizard_step = fields.Selection(
        [('select', 'Select Day'), ('confirm', 'Confirm')],
        string='Step',
        default='select',
        required=True,
    )
    payment_day = fields.Integer(string='Payment Day', required=True, default=lambda self: self._default_payment_day())
    currency_id = fields.Many2one(related='subscription_id.currency_id', readonly=True)
    current_next_invoice_date = fields.Date(related='subscription_id.next_invoice_date', readonly=True)
    stub_start_date = fields.Date(compute='_compute_dates', store=False)
    stub_end_date = fields.Date(compute='_compute_dates', store=False)
    new_next_invoice_date = fields.Date(compute='_compute_dates', store=False)
    stub_days = fields.Integer(compute='_compute_dates', store=False)
    full_period_days = fields.Integer(compute='_compute_dates', store=False)
    stub_ratio = fields.Float(compute='_compute_dates', store=False)
    stub_amount = fields.Monetary(string='Prorated Total', compute='_compute_dates', store=False, currency_field='currency_id')
    checklist_customer_approved = fields.Boolean(string='Customer approved the new billing day')
    checklist_explained_next_invoice = fields.Boolean(string='Agent explained the next invoice date')
    checklist_customer_understands_recurring = fields.Boolean(string='Customer understands future invoices will follow the new day')
    show_advanced = fields.Boolean(string='Show Advanced Details')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        # Force the wizard to start on the Select step even if a prior context or cache leaks a value
        res['wizard_step'] = 'select'
        res.setdefault('show_advanced', False)
        subscription_id = res.get('subscription_id')
        if subscription_id:
            subscription = self.env['sale.order'].browse(subscription_id)
            self._validate_change_window(subscription)
        return res

    @api.model_create_multi
    def create(self, vals_list):
        # Ensure wizard always starts on the select step even if context/defaults are missing
        for vals in vals_list:
            if not vals.get('wizard_step'):
                vals['wizard_step'] = 'select'
            vals.setdefault('show_advanced', False)
        return super().create(vals_list)

    @api.model
    def _default_subscription_id(self):
        ctx = self.env.context or {}
        return ctx.get('default_subscription_id')

    @api.model
    def _default_payment_day(self):
        ctx = self.env.context or {}
        subscription_id = ctx.get('default_subscription_id')
        subscription = self.env['sale.order'].browse(subscription_id) if subscription_id else None
        if subscription and subscription.exists():
            base_date = subscription.next_invoice_date or fields.Date.context_today(subscription)
            return base_date.day
        return fields.Date.context_today(self).day

    @api.constrains('payment_day')
    def _check_payment_day(self):
        for wizard in self:
            if wizard.payment_day < 1 or wizard.payment_day > 28:
                raise ValidationError(_('Payment day must be between 1 and 28 to avoid month-end issues.'))

    def _validate_change_window(self, subscription, stub_start=False):
        if not subscription:
            return

        today = fields.Date.context_today(self)
        stub_start = stub_start or subscription.next_invoice_date or subscription.start_date or today
        last_change = subscription.payment_change_log_ids.sorted('change_date', reverse=True)[:1]
        if last_change:
            last_change_date = last_change.change_date.date() if last_change.change_date else False
            if last_change_date and stub_start and last_change_date >= stub_start:
                raise ValidationError(_('Only one payment day change is allowed per billing cycle. Last change was on %s.') % last_change_date)
            delta_days = (today - last_change_date).days if last_change_date else 0
            if delta_days < 30:
                raise ValidationError(_('Wait %s more day(s) before changing the payment day again.') % (30 - delta_days))

    def action_next_step(self):
        self.ensure_one()
        self._validate_change_window(self.subscription_id, self.stub_start_date or self.subscription_id.next_invoice_date)
        self.wizard_step = 'confirm'
        return self._action_reload_wizard()

    def action_previous_step(self):
        self.ensure_one()
        self.wizard_step = 'select'
        return self._action_reload_wizard()

    def action_toggle_advanced(self):
        self.ensure_one()
        self.show_advanced = not self.show_advanced
        return self._action_reload_wizard()

    def _action_reload_wizard(self):
        self.ensure_one()
        view = self.env.ref('contract_management.view_change_payment_date_wizard')
        return {
            'type': 'ir.actions.act_window',
            'name': _('Change Payment Day'),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'views': [(view.id, 'form')],
            'view_id': view.id,
            'target': 'new',
            'context': dict(self.env.context),
        }

    @api.depends('subscription_id', 'payment_day')
    def _compute_dates(self):
        today = fields.Date.context_today(self)
        for wizard in self:
            subscription = wizard.subscription_id
            wizard.stub_start_date = False
            wizard.stub_end_date = False
            wizard.new_next_invoice_date = False
            wizard.stub_days = 0
            wizard.full_period_days = 0
            wizard.stub_ratio = 0.0
            wizard.stub_amount = 0.0

            if not subscription:
                continue

            stub_start = subscription.next_invoice_date or subscription.start_date or today
            billing_delta = subscription._cm_get_billing_period_delta() if hasattr(subscription, '_cm_get_billing_period_delta') else relativedelta(months=1)
            stub_end_of_cycle = stub_start + billing_delta - timedelta(days=1)
            full_period_days = (stub_end_of_cycle - stub_start).days + 1

            payment_day = wizard.payment_day or stub_start.day
            desired_day = min(payment_day, calendar.monthrange(stub_start.year, stub_start.month)[1])
            next_date = subscription.next_invoice_date
            is_next_in_past = bool(next_date and next_date < today)
            if next_date and next_date.day == desired_day and not is_next_in_past:
                wizard.stub_start_date = stub_start
                wizard.stub_end_date = stub_start
                wizard.new_next_invoice_date = next_date
                wizard.stub_days = 0
                wizard.full_period_days = full_period_days
                wizard.stub_ratio = 0.0
                wizard.stub_amount = 0.0
                continue

            target_date = self._compute_target_date(stub_start, payment_day)
            stub_end = target_date - timedelta(days=1) if target_date else False
            stub_days = (target_date - stub_start).days if target_date else 0
            ratio = (stub_days / full_period_days) if full_period_days and stub_days > 0 else 0.0

            recurring_lines = subscription.order_line.filtered(lambda l: l.product_id.recurring_invoice)
            recurring_total = sum(recurring_lines.mapped('price_total')) if recurring_lines else 0.0

            wizard.stub_start_date = stub_start
            wizard.stub_end_date = stub_end
            wizard.new_next_invoice_date = target_date
            wizard.stub_days = stub_days
            wizard.full_period_days = full_period_days
            wizard.stub_ratio = ratio
            wizard.stub_amount = recurring_total * ratio

    def _compute_target_date(self, stub_start, payment_day):
        if not stub_start or not payment_day:
            return False

        def clamp_day(base_date):
            last_day = calendar.monthrange(base_date.year, base_date.month)[1]
            return min(payment_day, last_day)

        try:
            candidate = stub_start.replace(day=clamp_day(stub_start))
        except ValueError:
            return False

        if candidate <= stub_start:
            next_month = stub_start + relativedelta(months=1)
            candidate = next_month.replace(day=clamp_day(next_month))

        return candidate

    def action_confirm(self):
        self.ensure_one()
        if self.wizard_step != 'confirm':
            raise ValidationError(_('Review and confirm the change before submitting.'))
        subscription = self.subscription_id
        if not subscription:
            raise ValidationError(_('A subscription is required to change the payment day.'))

        today = fields.Date.context_today(self)
        if subscription.next_invoice_date:
            desired_day = min(self.payment_day, calendar.monthrange(subscription.next_invoice_date.year, subscription.next_invoice_date.month)[1])
            if subscription.next_invoice_date.day == desired_day and subscription.next_invoice_date >= today:
                raise ValidationError(_('Payment day is already %s; no prorated invoice is needed.') % desired_day)

        stub_start = self.stub_start_date or subscription.next_invoice_date or fields.Date.context_today(self)
        self._validate_change_window(subscription, stub_start)
        new_payment_date = self.new_next_invoice_date
        if not new_payment_date:
            raise ValidationError(_('Select a payment day that results in a valid future date.'))
        if new_payment_date <= stub_start:
            raise ValidationError(_('The next payment date must be after the last invoiced period.'))

        if self.stub_ratio <= 0:
            raise ValidationError(_('The prorated period must be greater than zero days.'))

        if not (self.checklist_customer_approved and self.checklist_explained_next_invoice and self.checklist_customer_understands_recurring):
            raise ValidationError(_('Please complete the confirmation checklist before proceeding.'))

        partner = subscription.partner_id.commercial_partner_id
        overdue_amount = partner.total_overdue or 0.0
        if overdue_amount > 0:
            currency = partner.currency_id or subscription.currency_id
            currency_name = currency.name if currency else ''
            amount_label = f"{overdue_amount:.2f} {currency_name}".strip()
            raise ValidationError(_('Cannot change payment day while the customer has an overdue balance (%s).') % amount_label)

        recurring_lines = subscription.order_line.filtered(lambda l: l.product_id.recurring_invoice)
        if not recurring_lines:
            raise ValidationError(_('This subscription has no recurring lines to invoice.'))

        previous_next_invoice_date = subscription.next_invoice_date

        invoice_vals = subscription._prepare_invoice()
        invoice_vals.update({
            'invoice_origin': f"{subscription.name} - payment day change",
            'invoice_date': fields.Date.context_today(self),
            'invoice_line_ids': [],
        })

        stub_end = self.stub_end_date or (new_payment_date - timedelta(days=1))

        for line in recurring_lines:
            line_vals = line._prepare_invoice_line()
            base_qty = line_vals.get('quantity', line.product_uom_qty)
            line_vals['quantity'] = (base_qty or 0.0) * self.stub_ratio
            line_vals['name'] = f"{line_vals.get('name', line.name)} (Prorated {self.stub_days}/{self.full_period_days} days: {stub_start} to {stub_end})"
            invoice_vals['invoice_line_ids'].append((0, 0, line_vals))

        invoice = self.env['account.move'].sudo().create(invoice_vals)
        invoice.action_post()

        subscription.sudo().write({'next_invoice_date': new_payment_date})
        self.env['payment.day.change.log'].sudo().create({
            'subscription_id': subscription.id,
            'previous_next_invoice_date': previous_next_invoice_date,
            'new_next_invoice_date': new_payment_date,
            'prorated_amount': invoice.amount_total,
            'currency_id': subscription.currency_id.id,
        })
        subscription.message_post(
            body=_('Payment day changed to %s. Stub invoice %s covers %s to %s (%s days).') % (
                new_payment_date,
                invoice.display_name,
                stub_start,
                stub_end,
                self.stub_days,
            )
        )

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': invoice.id,
            'view_mode': 'form',
            'target': 'current',
            'context': {'default_move_type': invoice.move_type},
        }


class ChangePaymentDateBatchWizard(models.TransientModel):
    _name = 'change.payment.date.batch.wizard'
    _description = 'Batch Change Payment Date Wizard'

    partner_id = fields.Many2one('res.partner', string='Customer', required=True)
    payment_day = fields.Integer(string='Payment Day', required=True, default=lambda self: fields.Date.context_today(self).day)
    subscription_ids = fields.Many2many('sale.order', string='Active Subscriptions', compute='_compute_subscriptions', readonly=True)
    subscription_count = fields.Integer(string='Subscription Count', compute='_compute_subscriptions', readonly=True)

    @api.depends('partner_id')
    def _compute_subscriptions(self):
        active_states = ['3_progress', '4_paused', '5_renewed']
        for wizard in self:
            partner = wizard.partner_id.commercial_partner_id if wizard.partner_id else False
            subscriptions = self.env['sale.order']
            if partner:
                subscriptions = self.env['sale.order'].search([
                    ('partner_id.commercial_partner_id', '=', partner.id),
                    ('is_subscription', '=', True),
                    ('subscription_state', 'in', active_states),
                ])
            wizard.subscription_ids = subscriptions
            wizard.subscription_count = len(subscriptions)

    @api.constrains('payment_day')
    def _check_payment_day(self):
        for wizard in self:
            if wizard.payment_day < 1 or wizard.payment_day > 28:
                raise ValidationError(_('Payment day must be between 1 and 28 to avoid month-end issues.'))

    def _compute_target_payment_date(self, stub_start, payment_day):
        if not stub_start or not payment_day:
            return False

        def clamp_day(base_date):
            last_day = calendar.monthrange(base_date.year, base_date.month)[1]
            return min(payment_day, last_day)

        try:
            candidate = stub_start.replace(day=clamp_day(stub_start))
        except ValueError:
            return False

        if candidate <= stub_start:
            next_month = stub_start + relativedelta(months=1)
            candidate = next_month.replace(day=clamp_day(next_month))

        return candidate

    def _validate_subscription(self, subscription):
        today = fields.Date.context_today(self)
        partner = subscription.partner_id.commercial_partner_id
        overdue_amount = partner.total_overdue or 0.0
        if overdue_amount > 0:
            currency = partner.currency_id or subscription.currency_id
            currency_name = currency.name if currency else ''
            amount_label = f"{overdue_amount:.2f} {currency_name}".strip()
            raise ValidationError(_('Cannot change payment day while the customer has an overdue balance (%s) for %s.') % (amount_label, subscription.display_name))

        last_change = subscription.payment_change_log_ids.sorted('change_date', reverse=True)[:1]
        if last_change:
            stub_start = subscription.next_invoice_date or subscription.start_date or today
            if stub_start and last_change.change_date and last_change.change_date.date() >= stub_start:
                raise ValidationError(_('Only one payment day change is allowed per billing cycle for %s. Last change was on %s.') % (subscription.display_name, last_change.change_date.date()))
            delta_days = (today - last_change.change_date.date()).days if last_change.change_date else 0
            if delta_days < 30:
                raise ValidationError(_('Wait %s more day(s) before changing the payment day again for %s.') % (30 - delta_days, subscription.display_name))

        recurring_lines = subscription.order_line.filtered(lambda l: l.product_id.recurring_invoice)
        if not recurring_lines:
            raise ValidationError(_('Subscription %s has no recurring lines to invoice.') % subscription.display_name)

    def _get_stub_info(self, subscription):
        self.ensure_one()
        today = fields.Date.context_today(self)
        stub_start = subscription.next_invoice_date or subscription.start_date or today
        billing_delta = subscription._cm_get_billing_period_delta() if hasattr(subscription, '_cm_get_billing_period_delta') else relativedelta(months=1)
        stub_end_of_cycle = stub_start + billing_delta - timedelta(days=1)
        full_period_days = (stub_end_of_cycle - stub_start).days + 1

        next_date = subscription.next_invoice_date
        is_next_in_past = bool(next_date and next_date < today)

        current_day = min(self.payment_day, calendar.monthrange(stub_start.year, stub_start.month)[1])
        if next_date and next_date.day == current_day and not is_next_in_past:
            return {
                'stub_start': stub_start,
                'stub_end': stub_start,
                'full_period_days': full_period_days,
                'stub_days': 0,
                'ratio': 0,
                'new_next_invoice_date': next_date,
                'needs_change': False,
            }

        target_date = self._compute_target_payment_date(stub_start, self.payment_day)
        if not target_date:
            raise ValidationError(_('Select a payment day that results in a valid future date for %s.') % subscription.display_name)

        # If the subscription is already aligned to this payment day, skip proration for it.
        if next_date and next_date == target_date and not is_next_in_past:
            return {
                'stub_start': stub_start,
                'stub_end': stub_start,
                'full_period_days': full_period_days,
                'stub_days': 0,
                'ratio': 0,
                'new_next_invoice_date': target_date,
                'needs_change': False,
            }

        if target_date <= stub_start:
            raise ValidationError(_('The next payment date must be after the last invoiced period for %s.') % subscription.display_name)

        stub_days = (target_date - stub_start).days
        ratio = (stub_days / full_period_days) if full_period_days and stub_days > 0 else 0.0
        if ratio <= 0:
            raise ValidationError(_('The prorated period must be greater than zero days for %s.') % subscription.display_name)
        stub_end = target_date - timedelta(days=1)
        return {
            'stub_start': stub_start,
            'stub_end': stub_end,
            'full_period_days': full_period_days,
            'stub_days': stub_days,
            'ratio': ratio,
            'new_next_invoice_date': target_date,
            'needs_change': True,
        }

    def action_confirm(self):
        self.ensure_one()
        partner = self.partner_id.commercial_partner_id or self.partner_id
        subscriptions = self.subscription_ids
        if not subscriptions:
            raise ValidationError(_('No active subscriptions found for this customer.'))

        invoice_vals = None
        invoice_lines = []
        stub_info_map = {}
        changed_subscriptions = []

        for subscription in subscriptions:
            stub_info = self._get_stub_info(subscription)

            # Skip subscriptions already aligned to the selected payment day
            if not stub_info.get('needs_change', True):
                continue

            self._validate_subscription(subscription)
            stub_info_map[subscription.id] = stub_info
            changed_subscriptions.append(subscription)

            if invoice_vals is None:
                invoice_vals = subscription._prepare_invoice()
                invoice_vals.update({
                    'partner_id': partner.id,
                    'invoice_origin': f"{partner.display_name} - payment day change (batch)",
                    'invoice_date': fields.Date.context_today(self),
                    'invoice_line_ids': [],
                })

            for line in subscription.order_line.filtered(lambda l: l.product_id.recurring_invoice):
                line_vals = line._prepare_invoice_line()
                base_qty = line_vals.get('quantity', line.product_uom_qty) or 0.0
                line_vals['quantity'] = base_qty * stub_info['ratio']
                line_vals['name'] = f"{line_vals.get('name', line.name)} ({subscription.name}: Prorated {stub_info['stub_days']}/{stub_info['full_period_days']} days: {stub_info['stub_start']} to {stub_info['stub_end']})"
                invoice_lines.append((0, 0, line_vals))

        if not invoice_lines or not invoice_vals:
            raise ValidationError(_('No subscriptions require a payment day change.'))

        invoice_vals['invoice_line_ids'] = invoice_lines
        invoice = self.env['account.move'].sudo().create(invoice_vals)
        invoice.action_post()

        for subscription in changed_subscriptions:
            info = stub_info_map.get(subscription.id)
            previous_date = subscription.next_invoice_date
            subscription.sudo().write({'next_invoice_date': info['new_next_invoice_date']})
            sub_lines = invoice.invoice_line_ids.filtered(lambda l: any(sl.order_id == subscription for sl in l.sale_line_ids))
            stub_amount = sum(sub_lines.mapped('price_total'))
            self.env['payment.day.change.log'].sudo().create({
                'subscription_id': subscription.id,
                'previous_next_invoice_date': previous_date,
                'new_next_invoice_date': info['new_next_invoice_date'],
                'prorated_amount': stub_amount,
                'currency_id': subscription.currency_id.id,
            })
            subscription.message_post(
                body=_('Payment day changed to %s via consolidated stub invoice %s covering %s to %s (%s days).') % (
                    info['new_next_invoice_date'],
                    invoice.display_name,
                    info['stub_start'],
                    info['stub_end'],
                    info['stub_days'],
                )
            )

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': invoice.id,
            'view_mode': 'form',
            'target': 'current',
            'context': {'default_move_type': invoice.move_type},
        }