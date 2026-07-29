from odoo.tests.common import TransactionCase


class TestResidentialInternetIptvReport(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        residential_report = cls.env.ref('contract_management.action_report_contract')
        iptv_report = cls.env.ref('contract_management.action_iptv_contract')
        cls.residential_category = cls.env['product.category'].create({
            'name': 'Residential Internet',
            'contract_template': residential_report.id,
        })
        cls.iptv_category = cls.env['product.category'].create({
            'name': 'IPTV',
            'contract_template': iptv_report.id,
        })
        cls.internet_product = cls.env['product.product'].create({
            'name': '$25 Residential Internet',
            'categ_id': cls.residential_category.id,
            'recurring_invoice': True,
            'list_price': 25.0,
        })
        cls.iptv_product = cls.env['product.product'].create({
            'name': 'IPTV Plan',
            'categ_id': cls.iptv_category.id,
            'recurring_invoice': True,
            'list_price': 10.0,
        })

    def _subscription(self, partner, product, price):
        return self.env['sale.order'].create({
            'partner_id': partner.id,
            'is_subscription': True,
            'subscription_state': '3_progress',
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_uom': product.uom_id.id,
                'product_uom_qty': 1.0,
                'price_unit': price,
            })],
        })

    def test_matches_services_across_different_subscriptions_and_contacts(self):
        customer = self.env['res.partner'].create({'name': 'Cross-sub Customer'})
        service_contact = self.env['res.partner'].create({
            'name': 'Cross-sub Service Address',
            'parent_id': customer.id,
            'type': 'other',
        })
        internet = self._subscription(customer, self.internet_product, 25.0)
        iptv = self._subscription(service_contact, self.iptv_product, 10.0)

        result = self.env['residential.internet.iptv.report'].search([
            ('partner_id', '=', customer.id),
        ])

        self.assertEqual(len(result), 1)
        self.assertFalse(result.same_subscription)
        self.assertIn(internet.name, result.internet_subscriptions)
        self.assertIn(iptv.name, result.iptv_subscriptions)
        self.assertEqual(result.internet_service_count, 1)
        self.assertEqual(result.iptv_account_count, 1)
        self.assertAlmostEqual(result.internet_monthly_amount, 25.0, places=2)
        self.assertAlmostEqual(result.iptv_monthly_amount, 10.0, places=2)
        self.assertAlmostEqual(result.total_monthly_payment, 35.0, places=2)

    def test_marks_services_on_the_same_subscription(self):
        customer = self.env['res.partner'].create({'name': 'Same-sub Customer'})
        subscription = self.env['sale.order'].create({
            'partner_id': customer.id,
            'is_subscription': True,
            'subscription_state': '3_progress',
            'order_line': [
                (0, 0, {
                    'product_id': self.internet_product.id,
                    'product_uom': self.internet_product.uom_id.id,
                    'product_uom_qty': 1.0,
                    'price_unit': 25.0,
                }),
                (0, 0, {
                    'product_id': self.iptv_product.id,
                    'product_uom': self.iptv_product.uom_id.id,
                    'product_uom_qty': 2.0,
                    'price_unit': 10.0,
                }),
            ],
        })

        result = self.env['residential.internet.iptv.report'].search([
            ('partner_id', '=', customer.id),
        ])

        self.assertEqual(len(result), 1)
        self.assertTrue(result.same_subscription)
        self.assertIn(subscription.name, result.internet_subscriptions)
        self.assertIn(subscription.name, result.iptv_subscriptions)
        self.assertEqual(result.internet_service_count, 1)
        self.assertEqual(result.iptv_account_count, 2)
        self.assertAlmostEqual(result.internet_monthly_amount, 25.0, places=2)
        self.assertAlmostEqual(result.iptv_monthly_amount, 20.0, places=2)
        self.assertAlmostEqual(result.total_monthly_payment, 45.0, places=2)

    def test_excludes_non_25_residential_plan(self):
        customer = self.env['res.partner'].create({'name': 'Other-price Customer'})
        self._subscription(customer, self.internet_product, 30.0)
        self._subscription(customer, self.iptv_product, 10.0)

        result = self.env['residential.internet.iptv.report'].search([
            ('partner_id', '=', customer.id),
        ])

        self.assertFalse(result)
