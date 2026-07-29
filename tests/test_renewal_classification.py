# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase


class TestRenewalClassification(TransactionCase):
    """Renewal change detection must follow Odoo's recurring-line copy rules."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env['res.partner'].create({
            'name': 'Renewal Classification Customer',
        })
        cls.recurring_product = cls.env['product.product'].create({
            'name': 'Recurring Internet Service',
            'recurring_invoice': True,
        })
        cls.other_recurring_product = cls.env['product.product'].create({
            'name': 'Different Recurring Internet Service',
            'recurring_invoice': True,
        })
        cls.installation_charge = cls.env['product.product'].create({
            'name': 'One-Time Installation Charge',
            'recurring_invoice': False,
        })

    def _create_order(self, products, **extra_values):
        values = {
            'partner_id': self.partner.id,
            'order_line': [
                (0, 0, {
                    'product_id': product.id,
                    'product_uom_qty': 1,
                })
                for product in products
            ],
        }
        values.update(extra_values)
        return self.env['sale.order'].create(values)

    def test_one_time_parent_line_does_not_trigger_installation(self):
        parent = self._create_order([
            self.recurring_product,
            self.installation_charge,
        ])

        renewal = self._create_order(
            [self.recurring_product],
            subscription_state='2_renewal',
            renewal_of_id=parent.id,
        )

        self.assertEqual(renewal.service_change_mode, 'no_change')
        self.assertEqual(renewal.installation_state, 'completed')
        self.assertEqual(renewal.configuration_state, 'completed')

    def test_recurring_product_change_still_requires_installation(self):
        parent = self._create_order([
            self.recurring_product,
            self.installation_charge,
        ])

        renewal = self._create_order(
            [self.other_recurring_product],
            subscription_state='2_renewal',
            renewal_of_id=parent.id,
        )

        self.assertEqual(renewal.service_change_mode, 'install_no_activation')
        self.assertEqual(renewal.installation_state, 'to_be_scheduled')
        self.assertEqual(renewal.configuration_state, 'to_be_scheduled')
