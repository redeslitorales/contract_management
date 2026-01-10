# -*- coding: utf-8 -*-
from odoo import models, fields, api
from datetime import datetime, timedelta


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
        ('expired', 'Expired'),
        ('terminated', 'Terminated'),
        ('renewal_due', 'Renewal Due'),
        ('signature_in_process', 'Signature In Process'),
        ('signed', 'Signed')
    ], string='Status Filter')
    
    # Summary statistics by status
    total_contracts = fields.Integer(string='Total Contracts', compute='_compute_statistics', store=False)
    total_draft = fields.Integer(string='Draft', compute='_compute_statistics', store=False)
    total_active = fields.Integer(string='Active', compute='_compute_statistics', store=False)
    total_signed = fields.Integer(string='Signed', compute='_compute_statistics', store=False)
    total_signature_in_process = fields.Integer(string='Signature In Process', compute='_compute_statistics', store=False)
    total_expired = fields.Integer(string='Expired', compute='_compute_statistics', store=False)
    total_terminated = fields.Integer(string='Terminated', compute='_compute_statistics', store=False)
    total_renewal_due = fields.Integer(string='Renewal Due', compute='_compute_statistics', store=False)
    
    # Financial summary
    total_contract_value = fields.Float(string='Total Contract Value', compute='_compute_statistics', store=False)
    avg_contract_value = fields.Float(string='Average Contract Value', compute='_compute_statistics', store=False)
    
    # Expiration tracking
    expiring_30_days = fields.Integer(string='Expiring in 30 Days', compute='_compute_statistics', store=False)
    expiring_60_days = fields.Integer(string='Expiring in 60 Days', compute='_compute_statistics', store=False)
    expiring_90_days = fields.Integer(string='Expiring in 90 Days', compute='_compute_statistics', store=False)
    
    # Expiring contract details
    expiring_30_days_list = fields.Text(string='Contracts Expiring in 30 Days', compute='_compute_statistics', store=False)
    expiring_60_days_list = fields.Text(string='Contracts Expiring in 60 Days', compute='_compute_statistics', store=False)
    expiring_90_days_list = fields.Text(string='Contracts Expiring in 90 Days', compute='_compute_statistics', store=False)
    
    # Top partners summary (JSON field for flexibility)
    top_partners_summary = fields.Text(string='Top Partners', compute='_compute_statistics', store=False)
    
    # Contract term distribution (JSON field)
    term_distribution = fields.Text(string='Contract Term Distribution', compute='_compute_statistics', store=False)

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
            dashboard.total_draft = len(contracts.filtered(lambda c: c.state == 'draft'))
            dashboard.total_active = len(contracts.filtered(lambda c: c.state == 'active'))
            dashboard.total_signed = len(contracts.filtered(lambda c: c.state == 'signed'))
            dashboard.total_signature_in_process = len(contracts.filtered(lambda c: c.state == 'signature_in_process'))
            dashboard.total_expired = len(contracts.filtered(lambda c: c.state == 'expired'))
            dashboard.total_terminated = len(contracts.filtered(lambda c: c.state == 'terminated'))
            dashboard.total_renewal_due = len(contracts.filtered(lambda c: c.state == 'renewal_due'))
            
            # Financial summary
            dashboard.total_contract_value = sum(contracts.mapped('total_paid'))
            dashboard.avg_contract_value = dashboard.total_contract_value / dashboard.total_contracts if dashboard.total_contracts > 0 else 0
            
            # Expiration tracking
            today = fields.Date.today()
            date_30 = today + timedelta(days=30)
            date_60 = today + timedelta(days=60)
            date_90 = today + timedelta(days=90)
            
            active_contracts = contracts.filtered(lambda c: c.state == 'active' and c.end_date)
            expiring_30 = active_contracts.filtered(lambda c: today <= c.end_date <= date_30)
            expiring_60 = active_contracts.filtered(lambda c: today <= c.end_date <= date_60)
            expiring_90 = active_contracts.filtered(lambda c: today <= c.end_date <= date_90)
            
            dashboard.expiring_30_days = len(expiring_30)
            dashboard.expiring_60_days = len(expiring_60)
            dashboard.expiring_90_days = len(expiring_90)
            
            # Generate detailed listings
            dashboard.expiring_30_days_list = dashboard._format_expiring_contracts(expiring_30)
            dashboard.expiring_60_days_list = dashboard._format_expiring_contracts(expiring_60)
            dashboard.expiring_90_days_list = dashboard._format_expiring_contracts(expiring_90)
            
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
            top_partners_text = '\n'.join([f"{name}: {data['count']} contracts (${data['value']:,.2f})" 
                                          for name, data in sorted_partners])
            dashboard.top_partners_summary = top_partners_text or 'No contracts'
            
            # Contract term distribution
            term_data = {}
            for contract in contracts:
                if contract.contract_term:
                    term_name = contract.contract_term.name
                    term_data[term_name] = term_data.get(term_name, 0) + 1
            
            sorted_terms = sorted(term_data.items(), key=lambda x: x[1], reverse=True)
            term_text = '\n'.join([f"{term}: {count} contracts" for term, count in sorted_terms])
            dashboard.term_distribution = term_text or 'No contracts'

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

    def action_view_signed_contracts(self):
        """Action to view signed contracts."""
        domain = self._get_filtered_domain()
        domain.append(('state', '=', 'signed'))
        return self._create_action('Signed Contracts', domain)

    def action_view_expired_contracts(self):
        """Action to view expired contracts."""
        domain = self._get_filtered_domain()
        domain.append(('state', '=', 'expired'))
        return self._create_action('Expired Contracts', domain)

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

    def action_refresh_statistics(self):
        """Manual refresh of statistics."""
        self._compute_statistics()
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }

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
        """Format contract list with partner, amount, and expiration date."""
        if not contracts:
            return 'No contracts expiring in this period'
        
        lines = []
        for contract in contracts.sorted(key=lambda c: c.end_date):
            partner_name = contract.partner_id.name if contract.partner_id else 'Unknown'
            amount = f"${contract.total_paid:,.2f}" if contract.total_paid else '$0.00'
            end_date = contract.end_date.strftime('%Y-%m-%d') if contract.end_date else 'N/A'
            contract_name = contract.name or f"Contract #{contract.id}"
            lines.append(f"{end_date} | {partner_name} | {contract_name} | {amount}")
        
        return '\n'.join(lines)
