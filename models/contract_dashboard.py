# -*- coding: utf-8 -*-
from odoo import models, fields, api
from datetime import datetime, timedelta

try:
    from .contract_management import (
        SUBSCRIPTION_ACTIVE_STATE,
        SUBSCRIPTION_DRAFT_STATE,
        SUBSCRIPTION_SUSPENDED_STATE,
    )
except Exception:
    SUBSCRIPTION_DRAFT_STATE = ['1_draft', '1a_pending', '1b_install', '1c_nocontract', '1d_internal', '1e_confirm', '2_renewal']
    SUBSCRIPTION_ACTIVE_STATE = ['3_progress', '4_paused', '5_renewed']
    SUBSCRIPTION_SUSPENDED_STATE = ['8_suspend']


class ContractDashboard(models.Model):
    _name = 'contract.dashboard'
    _description = 'Contract Management Dashboard'
    _order = 'id desc'

    name = fields.Char(string='Dashboard Name', required=True, default='Contract Overview')
    
    # Filter fields
    date_from = fields.Date(string='Start Date From')
    date_to = fields.Date(string='Start Date To')
    partner_id = fields.Many2one('res.partner', string='Customer')
    contract_term_id = fields.Many2one('dte.base.contract', string='Contract Term')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('renewal_due', 'Renewal Due'),
        ('expired', 'Expired'),
        ('terminated', 'Terminated')
    ], string='Status Filter')
    
    # Summary statistics by status
    total_contracts = fields.Integer(string='Total Contracts', compute='_compute_statistics', store=False)
    total_draft = fields.Integer(string='Draft', compute='_compute_statistics', store=False)
    total_active = fields.Integer(string='Active', compute='_compute_statistics', store=False)
    total_expired = fields.Integer(string='Expired', compute='_compute_statistics', store=False)
    total_terminated = fields.Integer(string='Terminated', compute='_compute_statistics', store=False)
    total_renewal_due = fields.Integer(string='Renewal Due', compute='_compute_statistics', store=False)
    total_value_draft = fields.Float(string='Draft Total Value', compute='_compute_statistics', store=False)
    total_value_active = fields.Float(string='Active Total Value', compute='_compute_statistics', store=False)
    total_value_renewal_due = fields.Float(string='Renewal Due Total Value', compute='_compute_statistics', store=False)
    total_value_expired = fields.Float(string='Expired Total Value', compute='_compute_statistics', store=False)
    total_value_terminated = fields.Float(string='Terminated Total Value', compute='_compute_statistics', store=False)
    avg_value_draft = fields.Float(string='Draft Avg Value', compute='_compute_statistics', store=False)
    avg_value_active = fields.Float(string='Active Avg Value', compute='_compute_statistics', store=False)
    avg_value_renewal_due = fields.Float(string='Renewal Due Avg Value', compute='_compute_statistics', store=False)
    avg_value_expired = fields.Float(string='Expired Avg Value', compute='_compute_statistics', store=False)
    avg_value_terminated = fields.Float(string='Terminated Avg Value', compute='_compute_statistics', store=False)
    state_summary_html = fields.Html(string='Contract Summary Table', compute='_compute_statistics', sanitize=False)
    total_sig_new = fields.Integer(string='Signature: New', compute='_compute_statistics', store=False)
    total_sig_sent = fields.Integer(string='Signature: Sent', compute='_compute_statistics', store=False)
    total_sig_open = fields.Integer(string='Signature: Open', compute='_compute_statistics', store=False)
    total_sig_customer = fields.Integer(string='Signature: Customer Signed', compute='_compute_statistics', store=False)
    total_sig_completed = fields.Integer(string='Signature: Completed', compute='_compute_statistics', store=False)
    total_value_sig_new = fields.Float(string='Signature New Total Value', compute='_compute_statistics', store=False)
    total_value_sig_sent = fields.Float(string='Signature Sent Total Value', compute='_compute_statistics', store=False)
    total_value_sig_open = fields.Float(string='Signature Open Total Value', compute='_compute_statistics', store=False)
    total_value_sig_customer = fields.Float(string='Signature Customer Total Value', compute='_compute_statistics', store=False)
    total_value_sig_completed = fields.Float(string='Signature Completed Total Value', compute='_compute_statistics', store=False)
    avg_value_sig_new = fields.Float(string='Signature New Avg Value', compute='_compute_statistics', store=False)
    avg_value_sig_sent = fields.Float(string='Signature Sent Avg Value', compute='_compute_statistics', store=False)
    avg_value_sig_open = fields.Float(string='Signature Open Avg Value', compute='_compute_statistics', store=False)
    avg_value_sig_customer = fields.Float(string='Signature Customer Avg Value', compute='_compute_statistics', store=False)
    avg_value_sig_completed = fields.Float(string='Signature Completed Avg Value', compute='_compute_statistics', store=False)
    signature_summary_html = fields.Html(string='Signature Summary Table', compute='_compute_statistics', sanitize=False)
    
    # Financial summary
    total_contract_value = fields.Float(string='Total Contract Value', compute='_compute_statistics', store=False)
    avg_contract_value = fields.Float(string='Average Contract Value', compute='_compute_statistics', store=False)
    
    # Expiration tracking
    expiring_30_days = fields.Integer(string='Expiring in 30 Days', compute='_compute_statistics', store=False)
    expiring_60_days = fields.Integer(string='Expiring in 60 Days', compute='_compute_statistics', store=False)
    expiring_90_days = fields.Integer(string='Expiring in 90 Days', compute='_compute_statistics', store=False)
    non_compliant_count = fields.Integer(string='Non Compliant', compute='_compute_statistics', store=False)
    
    # Expiring contract details
    expiring_30_days_list = fields.Html(string='Contracts Expiring in 30 Days', compute='_compute_statistics', sanitize=False, store=False)
    expiring_60_days_list = fields.Html(string='Contracts Expiring in 60 Days', compute='_compute_statistics', sanitize=False, store=False)
    expiring_90_days_list = fields.Html(string='Contracts Expiring in 90 Days', compute='_compute_statistics', sanitize=False, store=False)
    non_compliant_list = fields.Html(string='Non Compliant Contracts', compute='_compute_statistics', sanitize=False, store=False)
    
    # Top partners summary (JSON field for flexibility)
    top_partners_summary = fields.Html(string='Top Partners', compute='_compute_statistics', sanitize=False, store=False)
    
    # Contract term distribution (JSON field)
    term_distribution = fields.Html(string='Contract Term Distribution', compute='_compute_statistics', sanitize=False, store=False)

    @api.depends('date_from', 'date_to', 'partner_id', 'contract_term_id', 'state')
    def _compute_statistics(self):
        """Compute all dashboard statistics based on filters."""
        for dashboard in self:
            domain = []
            
            # Apply filters
            if dashboard.date_from:
                domain.append(('start_date', '>=', dashboard.date_from))
            if dashboard.date_to:
                domain.append(('start_date', '<=', dashboard.date_to))
            if dashboard.partner_id:
                domain.append(('partner_id', '=', dashboard.partner_id.id))
            if dashboard.contract_term_id:
                domain.append(('contract_term', '=', dashboard.contract_term_id.id))
            if dashboard.state:
                domain.append(('state', '=', dashboard.state))
            
            # Get contracts
            Contract = self.env['contract.management'].sudo()
            contracts = Contract.search(domain)
            
            # Basic counts by status
            dashboard.total_contracts = len(contracts)

            draft_contracts = contracts.filtered(lambda c: c.state == 'draft')
            active_contracts = contracts.filtered(lambda c: c.state == 'active')
            renewal_contracts = contracts.filtered(lambda c: c.state == 'renewal_due')
            expired_contracts = contracts.filtered(lambda c: c.state == 'expired')
            terminated_contracts = contracts.filtered(lambda c: c.state == 'terminated')

            dashboard.total_draft = len(draft_contracts)
            dashboard.total_active = len(active_contracts)
            dashboard.total_expired = len(expired_contracts)
            dashboard.total_terminated = len(terminated_contracts)
            dashboard.total_renewal_due = len(renewal_contracts)
            dashboard.total_value_draft = sum(draft_contracts.mapped('total_paid'))
            dashboard.total_value_active = sum(active_contracts.mapped('total_paid'))
            dashboard.total_value_renewal_due = sum(renewal_contracts.mapped('total_paid'))
            dashboard.total_value_expired = sum(expired_contracts.mapped('total_paid'))
            dashboard.total_value_terminated = sum(terminated_contracts.mapped('total_paid'))
            dashboard.avg_value_draft = dashboard.total_value_draft / dashboard.total_draft if dashboard.total_draft else 0
            dashboard.avg_value_active = dashboard.total_value_active / dashboard.total_active if dashboard.total_active else 0
            dashboard.avg_value_renewal_due = dashboard.total_value_renewal_due / dashboard.total_renewal_due if dashboard.total_renewal_due else 0
            dashboard.avg_value_expired = dashboard.total_value_expired / dashboard.total_expired if dashboard.total_expired else 0
            dashboard.avg_value_terminated = dashboard.total_value_terminated / dashboard.total_terminated if dashboard.total_terminated else 0

            sig_new_contracts = contracts.filtered(lambda c: c.docusign_status == 'new')
            sig_sent_contracts = contracts.filtered(lambda c: c.docusign_status == 'sent')
            sig_open_contracts = contracts.filtered(lambda c: c.docusign_status == 'open')
            sig_customer_contracts = contracts.filtered(lambda c: c.docusign_status == 'customer')
            sig_completed_contracts = contracts.filtered(lambda c: c.docusign_status == 'completed')

            dashboard.total_sig_new = len(sig_new_contracts)
            dashboard.total_sig_sent = len(sig_sent_contracts)
            dashboard.total_sig_open = len(sig_open_contracts)
            dashboard.total_sig_customer = len(sig_customer_contracts)
            dashboard.total_sig_completed = len(sig_completed_contracts)
            dashboard.total_value_sig_new = sum(sig_new_contracts.mapped('total_paid'))
            dashboard.total_value_sig_sent = sum(sig_sent_contracts.mapped('total_paid'))
            dashboard.total_value_sig_open = sum(sig_open_contracts.mapped('total_paid'))
            dashboard.total_value_sig_customer = sum(sig_customer_contracts.mapped('total_paid'))
            dashboard.total_value_sig_completed = sum(sig_completed_contracts.mapped('total_paid'))
            dashboard.avg_value_sig_new = dashboard.total_value_sig_new / dashboard.total_sig_new if dashboard.total_sig_new else 0
            dashboard.avg_value_sig_sent = dashboard.total_value_sig_sent / dashboard.total_sig_sent if dashboard.total_sig_sent else 0
            dashboard.avg_value_sig_open = dashboard.total_value_sig_open / dashboard.total_sig_open if dashboard.total_sig_open else 0
            dashboard.avg_value_sig_customer = dashboard.total_value_sig_customer / dashboard.total_sig_customer if dashboard.total_sig_customer else 0
            dashboard.avg_value_sig_completed = dashboard.total_value_sig_completed / dashboard.total_sig_completed if dashboard.total_sig_completed else 0
            
            # Financial summary
            dashboard.total_contract_value = sum(contracts.mapped('total_paid'))
            dashboard.avg_contract_value = dashboard.total_contract_value / dashboard.total_contracts if dashboard.total_contracts > 0 else 0

            dashboard.state_summary_html = dashboard._build_state_summary_table([
                ('Draft', dashboard.total_draft, dashboard.total_value_draft, dashboard.avg_value_draft, 'action_view_draft_contracts'),
                ('Active', dashboard.total_active, dashboard.total_value_active, dashboard.avg_value_active, 'action_view_active_contracts'),
                ('Renewal Due', dashboard.total_renewal_due, dashboard.total_value_renewal_due, dashboard.avg_value_renewal_due, 'action_view_renewal_due_contracts'),
                ('Expired', dashboard.total_expired, dashboard.total_value_expired, dashboard.avg_value_expired, 'action_view_expired_contracts'),
                ('Terminated', dashboard.total_terminated, dashboard.total_value_terminated, dashboard.avg_value_terminated, 'action_view_terminated_contracts'),
            ])

            dashboard.signature_summary_html = dashboard._build_state_summary_table([
                ('New', dashboard.total_sig_new, dashboard.total_value_sig_new, dashboard.avg_value_sig_new, 'action_view_sig_new'),
                ('Sent', dashboard.total_sig_sent, dashboard.total_value_sig_sent, dashboard.avg_value_sig_sent, 'action_view_sig_sent'),
                ('Customer Signed', dashboard.total_sig_customer, dashboard.total_value_sig_customer, dashboard.avg_value_sig_customer, 'action_view_sig_customer'),
                ('Completed', dashboard.total_sig_completed, dashboard.total_value_sig_completed, dashboard.avg_value_sig_completed, 'action_view_sig_completed'),
                ('Open', dashboard.total_sig_open, dashboard.total_value_sig_open, dashboard.avg_value_sig_open, 'action_view_sig_open'),
            ])
            
            # Expiration tracking
            today = fields.Date.today()
            date_30 = today + timedelta(days=30)
            date_60 = today + timedelta(days=60)
            date_90 = today + timedelta(days=90)
            
            active_end_dated_contracts = active_contracts.filtered(lambda c: c.end_date)
            expiring_30 = active_end_dated_contracts.filtered(lambda c: today <= c.end_date <= date_30)
            expiring_60 = active_end_dated_contracts.filtered(lambda c: today <= c.end_date <= date_60)
            expiring_90 = active_end_dated_contracts.filtered(lambda c: today <= c.end_date <= date_90)
            
            dashboard.expiring_30_days = len(expiring_30)
            dashboard.expiring_60_days = len(expiring_60)
            dashboard.expiring_90_days = len(expiring_90)
            
            # Generate detailed listings
            dashboard.expiring_30_days_list = dashboard._format_expiring_contracts(expiring_30)
            dashboard.expiring_60_days_list = dashboard._format_expiring_contracts(expiring_60)
            dashboard.expiring_90_days_list = dashboard._format_expiring_contracts(expiring_90)

            allowed_active_states = SUBSCRIPTION_ACTIVE_STATE + SUBSCRIPTION_SUSPENDED_STATE
            non_compliant_contracts = contracts.filtered(
                lambda c: (
                    c.state == 'draft'
                    and (c.subscription_id.subscription_state not in SUBSCRIPTION_DRAFT_STATE)
                )
                or (
                    c.state == 'active'
                    and (c.subscription_id.subscription_state not in allowed_active_states)
                )
            )

            dashboard.non_compliant_count = len(non_compliant_contracts)
            dashboard.non_compliant_list = dashboard._format_non_compliant_contracts(non_compliant_contracts)
            
            # Top partners by contract count
            partner_data = {}
            for contract in contracts:
                if contract.partner_id:
                    partner_name = contract.partner_id.name
                    if partner_name not in partner_data:
                        partner_data[partner_name] = {'count': 0, 'value': 0}
                    partner_data[partner_name]['count'] += 1
                    partner_data[partner_name]['value'] += contract.total_paid
            
            # Sort and get top 10
            sorted_partners = sorted(partner_data.items(), key=lambda x: x[1]['count'], reverse=True)[:10]
            dashboard.top_partners_summary = dashboard._format_top_partners(sorted_partners)
            
            # Contract term distribution
            term_data = {}
            for contract in contracts:
                if contract.contract_term:
                    term_name = contract.contract_term.name
                    term_data[term_name] = term_data.get(term_name, 0) + 1
            
            sorted_terms = sorted(term_data.items(), key=lambda x: x[1], reverse=True)
            dashboard.term_distribution = dashboard._format_term_distribution(sorted_terms)

    def action_view_draft_contracts(self):
        """Action to view draft contracts."""
        domain = self._get_filtered_domain()
        domain.append(('state', '=', 'draft'))
        return self._create_action('Draft Contracts', domain)

    def action_view_active_contracts(self):
        """Action to view active contracts."""
        domain = self._get_filtered_domain()
        domain.append(('state', '=', 'active'))
        return self._create_action('Active Contracts', domain)

    def action_view_expired_contracts(self):
        """Action to view expired contracts."""
        domain = self._get_filtered_domain()
        domain.append(('state', '=', 'expired'))
        return self._create_action('Expired Contracts', domain)

    def action_view_terminated_contracts(self):
        """Action to view terminated contracts."""
        domain = self._get_filtered_domain()
        domain.append(('state', '=', 'terminated'))
        return self._create_action('Terminated Contracts', domain)

    def action_view_renewal_due_contracts(self):
        """Action to view renewal due contracts."""
        domain = self._get_filtered_domain()
        domain.append(('state', '=', 'renewal_due'))
        return self._create_action('Renewal Due Contracts', domain)

    def action_view_expiring_30_days(self):
        """Action to view contracts expiring in 30 days."""
        today = fields.Date.today()
        date_30 = today + timedelta(days=30)
        domain = self._get_filtered_domain()
        domain.extend([
            ('state', '=', 'active'),
            ('end_date', '>=', today),
            ('end_date', '<=', date_30)
        ])
        return self._create_action('Expiring in 30 Days', domain)

    def action_view_expiring_60_days(self):
        """Action to view contracts expiring in 60 days."""
        today = fields.Date.today()
        date_60 = today + timedelta(days=60)
        domain = self._get_filtered_domain()
        domain.extend([
            ('state', '=', 'active'),
            ('end_date', '>=', today),
            ('end_date', '<=', date_60)
        ])
        return self._create_action('Expiring in 60 Days', domain)

    def action_view_expiring_90_days(self):
        """Action to view contracts expiring in 90 days."""
        today = fields.Date.today()
        date_90 = today + timedelta(days=90)
        domain = self._get_filtered_domain()
        domain.extend([
            ('state', '=', 'active'),
            ('end_date', '>=', today),
            ('end_date', '<=', date_90)
        ])
        return self._create_action('Expiring in 90 Days', domain)

    def action_view_non_compliant(self):
        """Action to view contracts whose subscription state conflicts with contract state."""
        domain = self._get_filtered_domain()
        allowed_active_states = SUBSCRIPTION_ACTIVE_STATE + SUBSCRIPTION_SUSPENDED_STATE
        domain.extend([
            '|',
            '&', ('state', '=', 'draft'), ('subscription_id.subscription_state', 'not in', SUBSCRIPTION_DRAFT_STATE),
            '&', ('state', '=', 'active'), ('subscription_id.subscription_state', 'not in', allowed_active_states),
        ])
        return self._create_action('Non Compliant Contracts', domain)

    def action_view_sig_new(self):
        domain = self._get_filtered_domain()
        domain.append(('docusign_status', '=', 'new'))
        return self._create_action('Signature: New', domain)

    def action_view_sig_sent(self):
        domain = self._get_filtered_domain()
        domain.append(('docusign_status', '=', 'sent'))
        return self._create_action('Signature: Sent', domain)

    def action_view_sig_open(self):
        domain = self._get_filtered_domain()
        domain.append(('docusign_status', '=', 'open'))
        return self._create_action('Signature: Open', domain)

    def action_view_sig_customer(self):
        domain = self._get_filtered_domain()
        domain.append(('docusign_status', '=', 'customer'))
        return self._create_action('Signature: Customer Signed', domain)

    def action_view_sig_completed(self):
        domain = self._get_filtered_domain()
        domain.append(('docusign_status', '=', 'completed'))
        return self._create_action('Signature: Completed', domain)

    def _get_filtered_domain(self):
        """Build domain based on dashboard filters."""
        domain = []
        if self.date_from:
            domain.append(('start_date', '>=', self.date_from))
        if self.date_to:
            domain.append(('start_date', '<=', self.date_to))
        if self.partner_id:
            domain.append(('partner_id', '=', self.partner_id.id))
        if self.contract_term_id:
            domain.append(('contract_term', '=', self.contract_term_id.id))
        if self.state:
            domain.append(('state', '=', self.state))
        return domain

    def _create_action(self, name, domain):
        """Create a window action to display contracts."""
        return {
            'type': 'ir.actions.act_window',
            'name': name,
            'res_model': 'contract.management',
            'view_mode': 'tree,form',
            'domain': domain,
            'context': {'create': False},
            'target': 'current',
        }
    
    def _format_expiring_contracts(self, contracts):
        """Format contract list as an HTML table with partner, amount, and expiration date."""
        if not contracts:
            return '<p>No contracts expiring in this period</p>'

        rows = []
        for contract in contracts.sorted(key=lambda c: c.end_date):
            partner_name = contract.partner_id.name if contract.partner_id else 'Unknown'
            amount = f"${contract.total_paid:,.2f}" if contract.total_paid else '$0.00'
            end_date = contract.end_date.strftime('%Y-%m-%d') if contract.end_date else 'N/A'
            contract_name = contract.name or f"Contract #{contract.id}"
            rows.append(
                f"<tr><td>{end_date}</td><td>{partner_name}</td><td>{contract_name}</td><td class='num'>{amount}</td></tr>"
            )

        header = (
            "<div style='width:100%;overflow-x:auto;'>"
            "<table class='o_table o_list_view o_contract_table' style='width:100%;table-layout:auto;min-width:900px;'>"
            "<thead><tr><th>End Date</th><th>Partner</th><th>Contract</th><th>Amount</th></tr></thead>"
            "<tbody>"
        )
        return header + ''.join(rows) + "</tbody></table></div>"

    def _format_non_compliant_contracts(self, contracts):
        """Format non-compliant contract list as an HTML table."""
        if not contracts:
            return '<p>No non compliant contracts</p>'

        state_selection = dict(self.env['contract.management']._fields['state'].selection)
        subscription_selection = dict(self.env['sale.order']._fields['subscription_state'].selection)
        signature_selection = dict(self.env['docusign.connector']._fields['state'].selection)

        def _label(value, selection_map):
            if not value:
                return 'N/A'
            return selection_map.get(value, value)

        def _badge(label, color):
            return (
                "<span style='display:inline-block;padding:2px 8px;border-radius:12px;font-size:12px;"
                "font-weight:600;background:" + color + ";color:#fff;'>" + label + "</span>"
            )

        def _pill_for_contract_state(value):
            label = _label(value, state_selection)
            color_map = {
                'draft': '#7f8c8d',
                'active': '#27ae60',
                'renewal_due': '#2980b9',
                'expired': '#8e44ad',
                'terminated': '#c0392b',
            }
            return _badge(label, color_map.get(value, '#7f8c8d'))

        def _pill_for_subscription_state(value):
            label = _label(value, subscription_selection)
            color_map = {
                '1_draft': '#7f8c8d',
                '1a_pending': '#3498db',
                '1b_install': '#3498db',
                '1c_nocontract': '#3498db',
                '1d_internal': '#3498db',
                '1e_confirm': '#3498db',
                '2_renewal': '#2980b9',
                '3_progress': '#27ae60',
                '4_paused': '#e67e22',
                '5_renewed': '#16a085',
                '6_churn': '#c0392b',
                '7_upsell': '#9b59b6',
                '8_suspend': '#d35400',
            }
            return _badge(label, color_map.get(value, '#7f8c8d'))

        def _pill_for_signature_state(value):
            label = _label(value, signature_selection)
            color_map = {
                'new': '#7f8c8d',
                'open': '#2980b9',
                'sent': '#8e44ad',
                'customer': '#e67e22',
                'completed': '#27ae60',
            }
            return _badge(label, color_map.get(value, '#7f8c8d'))

        def _link(model, rec_id, label):
            if not rec_id:
                return label
            url = f"/web#id={rec_id}&model={model}&view_type=form"
            return f"<a href='{url}' target='_blank'>{label}</a>"

        rows = []
        for contract in contracts.sorted(key=lambda c: (c.partner_id.name or '', c.name or '')):
            partner_name = contract.partner_id.name if contract.partner_id else 'Unknown'
            contract_name = contract.name or f"Contract #{contract.id}"
            end_date = contract.end_date.strftime('%Y-%m-%d') if contract.end_date else 'N/A'
            contract_state = contract.state or 'N/A'
            subscription_state = contract.subscription_id.subscription_state or 'N/A'
            signature_state = contract.docusign_status or 'N/A'
            partner_cell = _link('res.partner', contract.partner_id.id if contract.partner_id else False, partner_name)
            contract_cell = _link('contract.management', contract.id, contract_name)
            rows.append(
                "<tr>"
                f"<td>{partner_cell}</td>"
                f"<td>{contract_cell}</td>"
                f"<td>{end_date}</td>"
                f"<td>{_pill_for_contract_state(contract_state)}</td>"
                f"<td>{_pill_for_subscription_state(subscription_state)}</td>"
                f"<td>{_pill_for_signature_state(signature_state)}</td>"
                "</tr>"
            )

        header = (
            "<div style='width:100%;overflow-x:auto;'>"
            "<table class='o_table o_list_view o_contract_table' style='width:100%;table-layout:auto;min-width:1100px;'>"
            "<thead><tr><th>Partner</th><th>Contract</th><th>End Date</th><th>Contract State</th><th>Subscription State</th><th>Signature State</th></tr></thead>"
            "<tbody>"
        )
        return header + ''.join(rows) + "</tbody></table></div>"

    def _format_top_partners(self, partners):
        """Format top partners (name, count, value) as an HTML table."""
        if not partners:
            return '<p>No contracts</p>'

        rows = []
        for name, data in partners:
            count = data.get('count', 0)
            value = data.get('value', 0)
            rows.append(
                "<tr>"
                f"<td>{name}</td>"
                f"<td class='num'>{count}</td>"
                f"<td class='num'>${value:,.2f}</td>"
                "</tr>"
            )

        header = (
            "<div style='width:100%;overflow-x:auto;'>"
            "<table class='o_table o_list_view o_contract_table' style='width:100%;table-layout:auto;min-width:700px;'>"
            "<thead><tr><th>Partner</th><th>Contracts</th><th>Total Value</th></tr></thead>"
            "<tbody>"
        )
        return header + ''.join(rows) + "</tbody></table></div>"

    def _format_term_distribution(self, terms):
        """Format contract term distribution as an HTML table."""
        if not terms:
            return '<p>No contracts</p>'

        rows = [
            "<tr>" f"<td>{term}</td>" f"<td class='num'>{count}</td>" "</tr>"
            for term, count in terms
        ]

        header = (
            "<div style='width:100%;overflow-x:auto;'>"
            "<table class='o_table o_list_view o_contract_table' style='width:100%;table-layout:auto;min-width:600px;'>"
            "<thead><tr><th>Contract Term</th><th>Contracts</th></tr></thead>"
            "<tbody>"
        )
        return header + ''.join(rows) + "</tbody></table></div>"

    def _build_state_summary_table(self, summary_rows):
        """Build a HTML table with clickable column headers that trigger record actions."""
        dashboard_id = self.id or 0

        style_block = (
            "<style>"
            ".cm-summary-table table { width: 100%; border-collapse: collapse; table-layout: auto; }"
            ".cm-summary-table th, .cm-summary-table td { padding: 6px 8px; white-space: nowrap; }"
            ".cm-summary-table th { text-align: left; }"
            ".cm-summary-table td.num { text-align: right; }"
            ".cm-summary-table a.cm-status-link { text-decoration: none; font-weight: 600; }"
            "</style>"
        )

        rows_html = ''.join([
            (
                '<tr>'
                f'<td>{link_html}</td>'
                f'<td class="num">{count}</td>'
                f'<td class="num">${total:,.2f}</td>'
                f'<td class="num">${avg:,.2f}</td>'
                '</tr>'
            )
            for label, count, total, avg, action in summary_rows
            for link_html in [
                f'<a href="#" class="cm-status-link" data-action="{action}" data-dashboard="{dashboard_id}">{label}</a>'
                if action else label
            ]
        ])

        table = (
            f'<div class="cm-summary-table" data-dashboard="{dashboard_id}" style="width:100%;">'
            + style_block +
            '<table class="o_table o_contract_summary_table">'
            '<thead><tr>'
            '<th>Status</th>'
            '<th>Number of Contracts</th>'
            '<th>Total Value</th>'
            '<th>Average Value</th>'
            '</tr></thead>'
            '<tbody>' + rows_html + '</tbody>'
            '</table>'
            '</div>'
            f"""
            <script>
            (function() {{
                try {{
                    if (!window.odoo || !odoo.require) {{ return; }}
                    const rpc = odoo.require('web.rpc');
                    const {{ registry }} = odoo.require('@web/core/registry');
                    const actionService = (registry && registry.category('services').get('action'))
                        || (odoo.__DEBUG__ && odoo.__DEBUG__.services && (odoo.__DEBUG__.services.action || odoo.__DEBUG__.services['action_manager']));
                    const root = document.querySelector('.cm-summary-table[data-dashboard="{dashboard_id}"]');
                    if (!root) {{ return; }}
                    root.querySelectorAll('a.cm-status-link').forEach((link) => {{
                        link.addEventListener('click', (ev) => {{
                            ev.preventDefault();
                            const actionName = link.dataset.action;
                            if (!actionName) {{ return; }}
                            rpc.query({{
                                model: 'contract.dashboard',
                                method: actionName,
                                args: [[{dashboard_id}]],
                            }}).then((action) => {{
                                if (actionService && action) {{
                                    actionService.doAction(action);
                                }}
                            }}).catch((error) => {{
                                console.error('Dashboard header RPC error', error);
                            }});
                        }});
                    }});
                }} catch (err) {{
                    console.error('Dashboard header link error', err);
                }}
            }})();
            </script>
            """
        )
        return table

    def action_refresh_statistics(self):
        """Refresh dashboard statistics safely even if the current record was deleted."""
        dashboard = self[:1].exists()
        if not dashboard:
            dashboard = self.create({'name': 'Contract Overview'})

        new_dashboard = self.create({
            'name': dashboard.name,
            'date_from': dashboard.date_from,
            'date_to': dashboard.date_to,
            'partner_id': dashboard.partner_id.id if dashboard.partner_id else False,
            'contract_term_id': dashboard.contract_term_id.id if dashboard.contract_term_id else False,
            'state': dashboard.state,
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'contract.dashboard',
            'view_mode': 'form',
            'res_id': new_dashboard.id,
            'target': 'current',
        }

