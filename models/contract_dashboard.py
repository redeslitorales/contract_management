# -*- coding: utf-8 -*-
from odoo import models, fields, api
from collections import defaultdict
from datetime import timedelta

from markupsafe import escape

try:
    from .contract_management import (
        SUBSCRIPTION_ACTIVE_STATE,
        SUBSCRIPTION_DRAFT_STATE,
        SUBSCRIPTION_SUSPENDED_STATE,
    )
except Exception:
    SUBSCRIPTION_DRAFT_STATE = ['1_draft', '2_renewal', '7_upsell']
    SUBSCRIPTION_ACTIVE_STATE = ['3_progress', '4_paused', '5_renewed']
    SUBSCRIPTION_SUSPENDED_STATE = ['8_suspend']


class ContractDashboard(models.Model):
    _name = 'contract.dashboard'
    _description = 'Contract Management Dashboard'
    _order = 'id desc'

    _dashboard_detail_limit = 50

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
    progress_stage_summary_html = fields.Html(string='Progress Stage Summary', compute='_compute_statistics', sanitize=False)
    
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
        """Compute dashboard data without materializing every contract record.

        The old implementation repeatedly filtered one large recordset and read the
        non-stored ``total_paid`` field for every contract.  That field walks paid
        invoices and invoice lines, so dashboard time grew very quickly with the
        portfolio.  This implementation fetches the filtered ids once and lets
        PostgreSQL calculate paid totals in one set-based query.
        """
        for dashboard in self:
            Contract = self.env['contract.management'].sudo()
            contract_ids = Contract.search(dashboard._get_filtered_domain()).ids
            rows = dashboard._get_dashboard_rows(contract_ids)

            state_metrics = defaultdict(lambda: {'count': 0, 'value': 0.0})
            signature_metrics = defaultdict(lambda: {'count': 0, 'value': 0.0})
            stage_metrics = defaultdict(lambda: {'count': 0, 'value': 0.0})
            partner_metrics = defaultdict(lambda: {'count': 0, 'value': 0.0})
            term_counts = defaultdict(int)

            for row in rows:
                paid = row['total_paid'] or 0.0
                for metrics, key in (
                    (state_metrics, row['state']),
                    (signature_metrics, row['docusign_status']),
                    (stage_metrics, row['progress_stage']),
                ):
                    metrics[key]['count'] += 1
                    metrics[key]['value'] += paid
                if row['partner_id']:
                    partner_metrics[row['partner_name'] or 'Unknown']['count'] += 1
                    partner_metrics[row['partner_name'] or 'Unknown']['value'] += paid
                if row['contract_term_id']:
                    term_counts[row['contract_term_id']] += 1

            dashboard.total_contracts = len(rows)
            dashboard.total_contract_value = sum(row['total_paid'] or 0.0 for row in rows)
            dashboard.avg_contract_value = (
                dashboard.total_contract_value / dashboard.total_contracts
                if dashboard.total_contracts else 0.0
            )

            for code in ('draft', 'active', 'renewal_due', 'expired', 'terminated'):
                metric = state_metrics[code]
                setattr(dashboard, f'total_{code}', metric['count'])
                setattr(dashboard, f'total_value_{code}', metric['value'])
                setattr(
                    dashboard,
                    f'avg_value_{code}',
                    metric['value'] / metric['count'] if metric['count'] else 0.0,
                )

            for code in ('new', 'sent', 'open', 'customer', 'completed'):
                metric = signature_metrics[code]
                setattr(dashboard, f'total_sig_{code}', metric['count'])
                setattr(dashboard, f'total_value_sig_{code}', metric['value'])
                setattr(
                    dashboard,
                    f'avg_value_sig_{code}',
                    metric['value'] / metric['count'] if metric['count'] else 0.0,
                )

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

            stage_definitions = [
                ('Draft', 'draft', 'action_view_stage_draft'),
                ('Confirmed', 'confirmed', 'action_view_stage_confirmed'),
                ('Pending Contract', 'pending_contract', 'action_view_stage_pending_contract'),
                ('Pending Client Sign', 'pending_client_signature', 'action_view_stage_pending_client_signature'),
                ('Schedule Install/Config', 'schedule_install', 'action_view_stage_schedule_install'),
                ('Pending Install/Config', 'pending_install', 'action_view_stage_pending_install'),
                ('Pending Activation', 'pending_activation', 'action_view_stage_pending_activation'),
                ('Active', 'active', 'action_view_stage_active'),
                ('Renewed', 'renewed', 'action_view_stage_renewed'),
                ('Paused', 'paused', 'action_view_stage_paused'),
                ('Suspended', 'suspended', 'action_view_stage_suspended'),
                ('Churned', 'churned', 'action_view_stage_churned'),
                ('Active w/ Issues', 'active_with_issues', 'action_view_stage_active_with_issues'),
                ('Paused w/ Issues', 'paused_with_issues', 'action_view_stage_paused_with_issues'),
                ('Suspended w/ Issues', 'suspended_with_issues', 'action_view_stage_suspended_with_issues'),
            ]

            stage_rows = []
            for label, code, action in stage_definitions:
                metric = stage_metrics[code]
                stage_rows.append((
                    label,
                    metric['count'],
                    metric['value'],
                    metric['value'] / metric['count'] if metric['count'] else 0.0,
                    action,
                ))

            dashboard.progress_stage_summary_html = dashboard._build_state_summary_table(stage_rows)
            
            # Expiration tracking
            today = fields.Date.today()
            date_30 = today + timedelta(days=30)
            date_60 = today + timedelta(days=60)
            date_90 = today + timedelta(days=90)
            
            active_with_end = [row for row in rows if row['state'] == 'active' and row['end_date']]
            expiring_30 = [row for row in active_with_end if today <= row['end_date'] <= date_30]
            expiring_60 = [row for row in active_with_end if today <= row['end_date'] <= date_60]
            expiring_90 = [row for row in active_with_end if today <= row['end_date'] <= date_90]

            dashboard.expiring_30_days = len(expiring_30)
            dashboard.expiring_60_days = len(expiring_60)
            dashboard.expiring_90_days = len(expiring_90)
            dashboard.expiring_30_days_list = dashboard._format_expiring_rows(expiring_30)
            dashboard.expiring_60_days_list = dashboard._format_expiring_rows(expiring_60)
            dashboard.expiring_90_days_list = dashboard._format_expiring_rows(expiring_90)

            allowed_active_states = SUBSCRIPTION_ACTIVE_STATE + SUBSCRIPTION_SUSPENDED_STATE
            non_compliant_rows = [
                row for row in rows
                if (
                    row['state'] == 'draft'
                    and row['subscription_state'] not in SUBSCRIPTION_DRAFT_STATE
                ) or (
                    row['state'] == 'active'
                    and row['subscription_state'] not in allowed_active_states
                )
            ]
            dashboard.non_compliant_count = len(non_compliant_rows)
            dashboard.non_compliant_list = dashboard._format_non_compliant_rows(non_compliant_rows)

            sorted_partners = sorted(
                partner_metrics.items(), key=lambda item: item[1]['count'], reverse=True
            )[:10]
            dashboard.top_partners_summary = dashboard._format_top_partners(sorted_partners)

            term_names = {
                term.id: term.display_name
                for term in self.env['dte.base.contract'].sudo().browse(term_counts.keys()).exists()
            }
            sorted_terms = sorted(
                ((term_names.get(term_id, 'Unknown'), count) for term_id, count in term_counts.items()),
                key=lambda item: item[1],
                reverse=True,
            )
            dashboard.term_distribution = dashboard._format_term_distribution(sorted_terms)

    def _get_dashboard_rows(self, contract_ids):
        """Return one compact row per contract, including paid totals.

        ``total_paid`` is intentionally non-stored on the contract model.  Computing
        it record-by-record traverses every invoice relationship.  The CTE below is
        equivalent to that computation, but performs it once for the whole dashboard.
        """
        if not contract_ids:
            return []
        self.env.cr.execute("""
            WITH filtered_contracts AS (
                SELECT id, state, docusign_status, progress_stage, end_date, subscription_id
                  FROM contract_management
                 WHERE id = ANY(%s)
            ),
            eligible_invoices AS (
                SELECT DISTINCT sale_line.order_id,
                                invoice.id AS invoice_id,
                                invoice.amount_total
                  FROM sale_order_line_invoice_rel line_rel
                  JOIN sale_order_line sale_line
                    ON sale_line.id = line_rel.order_line_id
                  JOIN filtered_contracts contract
                    ON contract.subscription_id = sale_line.order_id
                  JOIN account_move_line invoice_line
                    ON invoice_line.id = line_rel.invoice_line_id
                  JOIN account_move invoice ON invoice.id = invoice_line.move_id
                 WHERE invoice.move_type = 'out_invoice'
                   AND invoice.state = 'posted'
                   AND invoice.payment_state = 'paid'
            ),
            recurring_lines AS (
                SELECT DISTINCT
                       line.move_id AS invoice_id,
                       line.id AS line_id,
                       line.price_total
                  FROM account_move_line line
                  JOIN eligible_invoices eligible ON eligible.invoice_id = line.move_id
                  JOIN sale_order_line_invoice_rel line_rel
                    ON line_rel.invoice_line_id = line.id
                  JOIN sale_order_line sale_line
                    ON sale_line.id = line_rel.order_line_id
                  JOIN product_product product
                    ON product.id = sale_line.product_id
                  JOIN product_template template
                    ON template.id = product.product_tmpl_id
                 WHERE template.recurring_invoice = TRUE
            ),
            recurring_totals AS (
                SELECT invoice_id, SUM(price_total) AS total
                  FROM recurring_lines
                 GROUP BY invoice_id
            ),
            paid_by_order AS (
                SELECT eligible.order_id,
                       SUM(
                           CASE WHEN recurring.invoice_id IS NOT NULL
                                THEN recurring.total
                                ELSE eligible.amount_total
                           END
                       ) AS total_paid
                  FROM eligible_invoices eligible
                  LEFT JOIN recurring_totals recurring ON recurring.invoice_id = eligible.invoice_id
                 GROUP BY eligible.order_id
            )
            SELECT contract.id,
                   contract.state,
                   contract.docusign_status,
                   contract.progress_stage,
                   contract.end_date,
                   contract.subscription_id,
                   subscription.subscription_state,
                   subscription.partner_id,
                   partner.name AS partner_name,
                   subscription.contract_term AS contract_term_id,
                   subscription.cabal_sequence AS contract_name,
                   COALESCE(paid.total_paid, 0.0) AS total_paid
              FROM filtered_contracts contract
              LEFT JOIN sale_order subscription ON subscription.id = contract.subscription_id
              LEFT JOIN res_partner partner ON partner.id = subscription.partner_id
              LEFT JOIN paid_by_order paid ON paid.order_id = contract.subscription_id
        """, [contract_ids])
        return self.env.cr.dictfetchall()

    def action_view_draft_contracts(self):
        """Action to view draft contracts."""
        domain = self._get_filtered_domain()
        domain.append(('state', '=', 'draft'))
        return self._create_action('Draft Contracts', domain)

    def action_view_all_contracts(self):
        """Open every contract matching the dashboard filters."""
        return self._create_action('All Contracts', self._get_filtered_domain())

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

    # Progress stage actions
    def _action_view_progress_stage(self, stage_code, title):
        domain = self._get_filtered_domain()
        domain.append(('progress_stage', '=', stage_code))
        return self._create_action(title, domain)

    def action_view_stage_draft(self):
        return self._action_view_progress_stage('draft', 'Progress: Draft')

    def action_view_stage_confirmed(self):
        return self._action_view_progress_stage('confirmed', 'Progress: Confirmed')

    def action_view_stage_pending_contract(self):
        return self._action_view_progress_stage('pending_contract', 'Progress: Pending Contract')

    def action_view_stage_pending_client_signature(self):
        return self._action_view_progress_stage('pending_client_signature', 'Progress: Pending Client Signature')

    def action_view_stage_schedule_install(self):
        return self._action_view_progress_stage('schedule_install', 'Progress: Schedule Install/Config')

    def action_view_stage_pending_install(self):
        return self._action_view_progress_stage('pending_install', 'Progress: Pending Install/Config')

    def action_view_stage_pending_activation(self):
        return self._action_view_progress_stage('pending_activation', 'Progress: Pending Activation')

    def action_view_stage_active(self):
        return self._action_view_progress_stage('active', 'Progress: Active')

    def action_view_stage_renewed(self):
        return self._action_view_progress_stage('renewed', 'Progress: Renewed')

    def action_view_stage_paused(self):
        return self._action_view_progress_stage('paused', 'Progress: Paused')

    def action_view_stage_suspended(self):
        return self._action_view_progress_stage('suspended', 'Progress: Suspended')

    def action_view_stage_churned(self):
        return self._action_view_progress_stage('churned', 'Progress: Churned')

    def action_view_stage_active_with_issues(self):
        return self._action_view_progress_stage('active_with_issues', 'Progress: Active w/ Issues')

    def action_view_stage_paused_with_issues(self):
        return self._action_view_progress_stage('paused_with_issues', 'Progress: Paused w/ Issues')

    def action_view_stage_suspended_with_issues(self):
        return self._action_view_progress_stage('suspended_with_issues', 'Progress: Suspended w/ Issues')

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

    def _format_expiring_rows(self, rows):
        """Render a bounded expiration preview from compact dashboard rows."""
        if not rows:
            return '<p class="cm-empty-state">No contracts expiring in this period</p>'

        ordered_rows = sorted(rows, key=lambda row: row['end_date'])
        visible_rows = ordered_rows[:self._dashboard_detail_limit]
        body = []
        for row in visible_rows:
            partner_name = escape(row['partner_name'] or 'Unknown')
            contract_name = escape(row['contract_name'] or f"Contract #{row['id']}")
            end_date = row['end_date'].strftime('%Y-%m-%d')
            body.append(
                '<tr>'
                f'<td><span class="cm-date-pill">{end_date}</span></td>'
                f'<td>{partner_name}</td>'
                f'<td><a href="/web#id={row["id"]}&amp;model=contract.management&amp;view_type=form">'
                f'{contract_name}</a></td>'
                f'<td class="num">${row["total_paid"] or 0.0:,.2f}</td>'
                '</tr>'
            )

        note = self._format_preview_note(len(ordered_rows))
        return (
            note
            + '<div class="cm-table-wrap"><table class="o_table cm-data-table">'
            '<thead><tr><th>End date</th><th>Customer</th><th>Contract</th><th>Paid</th></tr></thead>'
            '<tbody>' + ''.join(body) + '</tbody></table></div>'
        )

    def _format_non_compliant_rows(self, rows):
        """Render a bounded non-compliance preview from compact dashboard rows."""
        if not rows:
            return '<p class="cm-empty-state">All contracts are compliant</p>'

        state_selection = dict(self.env['contract.management']._fields['state'].selection)
        subscription_selection = dict(self.env['sale.order']._fields['subscription_state'].selection)
        signature_selection = dict(self.env['docusign.connector']._fields['state'].selection)
        visible_rows = sorted(
            rows,
            key=lambda row: ((row['partner_name'] or ''), (row['contract_name'] or '')),
        )[:self._dashboard_detail_limit]
        body = []
        for row in visible_rows:
            partner_name = escape(row['partner_name'] or 'Unknown')
            contract_name = escape(row['contract_name'] or f"Contract #{row['id']}")
            end_date = row['end_date'].strftime('%Y-%m-%d') if row['end_date'] else 'N/A'
            contract_label = escape(state_selection.get(row['state'], row['state'] or 'N/A'))
            subscription_label = escape(
                subscription_selection.get(
                    row['subscription_state'], row['subscription_state'] or 'N/A'
                )
            )
            signature_label = escape(
                signature_selection.get(row['docusign_status'], row['docusign_status'] or 'N/A')
            )
            body.append(
                '<tr>'
                f'<td>{partner_name}</td>'
                f'<td><a href="/web#id={row["id"]}&amp;model=contract.management&amp;view_type=form">'
                f'{contract_name}</a></td>'
                f'<td>{end_date}</td>'
                f'<td><span class="cm-state cm-state-{row["state"] or "unknown"}">{contract_label}</span></td>'
                f'<td><span class="cm-state cm-state-warning">{subscription_label}</span></td>'
                f'<td>{signature_label}</td>'
                '</tr>'
            )

        return (
            self._format_preview_note(len(rows))
            + '<div class="cm-table-wrap"><table class="o_table cm-data-table cm-data-table-wide">'
            '<thead><tr><th>Customer</th><th>Contract</th><th>End date</th>'
            '<th>Contract status</th><th>Subscription status</th><th>Signature</th></tr></thead>'
            '<tbody>' + ''.join(body) + '</tbody></table></div>'
        )

    def _format_preview_note(self, total):
        if total <= self._dashboard_detail_limit:
            return ''
        return (
            '<p class="cm-preview-note">Showing the first '
            f'{self._dashboard_detail_limit:,} of {total:,} results. '
            'Use the count above to open the full list.</p>'
        )
    
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
        """Reload the current dashboard without creating duplicate records."""
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }

