# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase
from odoo.exceptions import ValidationError
from datetime import date, timedelta


class TestContractDashboard(TransactionCase):
    """Test cases for Contract Dashboard functionality"""
    
    def setUp(self):
        super(TestContractDashboard, self).setUp()
        
        # Create test partner
        self.partner = self.env['res.partner'].create({
            'name': 'Test Customer Inc',
            'email': 'test@customer.com',
        })
        
        # Create test contract term
        self.contract_term = self.env['dte.base.contract'].create({
            'label': '12 Month Contract',
            'term': 12,
            'install_fee': 100.0,
        })
        
        # Create test contracts
        self.contract_draft = self.env['contract.management'].create({
            'state': 'draft',
            'start_date': date.today(),
            'subscription_id': self._create_subscription().id,
        })
        
        self.contract_active = self.env['contract.management'].create({
            'state': 'active',
            'start_date': date.today(),
            'end_date': date.today() + timedelta(days=20),  # Expiring in 20 days
            'subscription_id': self._create_subscription().id,
        })
        
        self.contract_expired = self.env['contract.management'].create({
            'state': 'expired',
            'start_date': date.today() - timedelta(days=400),
            'end_date': date.today() - timedelta(days=30),
            'subscription_id': self._create_subscription().id,
        })
        
        # Create dashboard
        self.dashboard = self.env['contract.dashboard'].create({
            'name': 'Test Dashboard',
        })
    
    def _create_subscription(self):
        """Helper to create minimal subscription"""
        return self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'date_order': date.today(),
        })
    
    def test_dashboard_creation(self):
        """Test dashboard record can be created"""
        self.assertTrue(self.dashboard.id)
        self.assertEqual(self.dashboard.name, 'Test Dashboard')
    
    def test_total_contracts_computation(self):
        """Test total contracts computes correctly"""
        self.dashboard._compute_statistics()
        # Should count all 3 test contracts (draft, active, expired)
        self.assertGreaterEqual(self.dashboard.total_contracts, 3)
    
    def test_status_counts_computation(self):
        """Test status-specific counts"""
        self.dashboard._compute_statistics()
        
        self.assertGreaterEqual(self.dashboard.total_draft, 1)
        self.assertGreaterEqual(self.dashboard.total_active, 1)
        self.assertGreaterEqual(self.dashboard.total_expired, 1)
    
    def test_expiring_30_days_computation(self):
        """Test expiring contracts within 30 days"""
        self.dashboard._compute_statistics()
        
        # contract_active expires in 20 days, should be counted
        self.assertGreaterEqual(self.dashboard.expiring_30_days, 1)
    
    def test_expiring_contracts_list_format(self):
        """Test expiring contracts list formatting"""
        self.dashboard._compute_statistics()
        
        # Check that listing contains date, partner, and amount
        listing = self.dashboard.expiring_30_days_list
        self.assertIn('|', listing)  # Should have pipe separators
        # Format: "2026-02-10 | Partner Name | Contract | $amount"
    
    def test_filter_by_partner(self):
        """Test filtering by specific partner"""
        self.dashboard.partner_id = self.partner.id
        self.dashboard._compute_statistics()
        
        # All test contracts have same partner
        self.assertGreaterEqual(self.dashboard.total_contracts, 3)
    
    def test_filter_by_status(self):
        """Test filtering by contract status"""
        self.dashboard.state = 'active'
        self.dashboard._compute_statistics()
        
        # Only active contracts should be counted
        self.assertEqual(self.dashboard.total_contracts, self.dashboard.total_active)
    
    def test_filter_by_date_range(self):
        """Test filtering by start date range"""
        self.dashboard.date_from = date.today()
        self.dashboard.date_to = date.today()
        self.dashboard._compute_statistics()
        
        # Only contracts starting today
        self.assertGreaterEqual(self.dashboard.total_contracts, 2)  # draft + active
    
    def test_financial_metrics(self):
        """Test total and average contract value"""
        # Add service to contract for value
        self.env['contract.service'].create({
            'name': 'Internet Service',
            'price': 100.0,
            'contract_id': self.contract_active.id,
            'product_id': self.env['product.product'].create({
                'name': 'Test Product',
                'list_price': 100.0,
            }).id,
        })
        
        self.dashboard._compute_statistics()
        
        self.assertGreaterEqual(self.dashboard.total_contract_value, 100.0)
        if self.dashboard.total_contracts > 0:
            self.assertGreater(self.dashboard.avg_contract_value, 0)
    
    def test_action_view_draft_contracts(self):
        """Test drill-down action for draft contracts"""
        action = self.dashboard.action_view_draft_contracts()
        
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'contract.management')
        self.assertIn(('state', '=', 'draft'), action['domain'])
    
    def test_action_view_expiring_30_days(self):
        """Test drill-down action for expiring contracts"""
        action = self.dashboard.action_view_expiring_30_days()
        
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertIn(('state', '=', 'active'), action['domain'])
        # Should filter by end_date within 30 days
    
    def test_action_refresh_statistics(self):
        """Test manual refresh action"""
        action = self.dashboard.action_refresh_statistics()
        
        # Should trigger reload
        self.assertEqual(action['type'], 'ir.actions.client')
        self.assertEqual(action['tag'], 'reload')
    
    def test_top_partners_summary(self):
        """Test top partners aggregation"""
        # Create another partner with more contracts
        partner2 = self.env['res.partner'].create({'name': 'Big Customer'})
        
        for i in range(5):
            self.env['contract.management'].create({
                'state': 'active',
                'start_date': date.today(),
                'subscription_id': self.env['sale.order'].create({
                    'partner_id': partner2.id,
                    'date_order': date.today(),
                }).id,
            })
        
        self.dashboard._compute_statistics()
        
        # Top partner should be in summary
        self.assertIn('Big Customer', self.dashboard.top_partners_summary)
        self.assertIn('contracts', self.dashboard.top_partners_summary)
    
    def test_term_distribution(self):
        """Test contract term distribution"""
        self.dashboard._compute_statistics()
        
        # Should show term distribution
        if self.dashboard.term_distribution != 'No contracts':
            self.assertIn('contracts', self.dashboard.term_distribution)
    
    def test_format_expiring_contracts_empty(self):
        """Test formatting with no expiring contracts"""
        empty_contracts = self.env['contract.management'].browse([])
        result = self.dashboard._format_expiring_contracts(empty_contracts)
        
        self.assertEqual(result, 'No contracts expiring in this period')
    
    def test_format_expiring_contracts_with_data(self):
        """Test formatting with actual contracts"""
        result = self.dashboard._format_expiring_contracts(self.contract_active)
        
        # Should contain date, partner, and amount
        self.assertIn('|', result)
        self.assertIn(self.partner.name, result)
    
    def test_filter_combination(self):
        """Test multiple filters applied together"""
        self.dashboard.partner_id = self.partner.id
        self.dashboard.state = 'active'
        self.dashboard.date_from = date.today()
        self.dashboard._compute_statistics()
        
        # Should respect all filters
        self.assertLessEqual(self.dashboard.total_contracts, self.dashboard.total_active)
    
    def test_expiring_60_and_90_days(self):
        """Test 60 and 90 day expiration tracking"""
        # Create contract expiring in 50 days
        contract_50 = self.env['contract.management'].create({
            'state': 'active',
            'start_date': date.today(),
            'end_date': date.today() + timedelta(days=50),
            'subscription_id': self._create_subscription().id,
        })
        
        self.dashboard._compute_statistics()
        
        # Should be in 60-day count
        self.assertGreaterEqual(self.dashboard.expiring_60_days, 1)
        # Should also be in 90-day count
        self.assertGreaterEqual(self.dashboard.expiring_90_days, 1)
