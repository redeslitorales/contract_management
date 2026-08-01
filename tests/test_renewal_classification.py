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

    def test_line_edit_is_reclassified_before_install_task_creation(self):
        attribute = self.env['product.attribute'].create({
            'name': 'Renewal Speed',
        })
        speed_50 = self.env['product.attribute.value'].create({
            'name': '50 Mbps',
            'attribute_id': attribute.id,
        })
        speed_150 = self.env['product.attribute.value'].create({
            'name': '150 Mbps',
            'attribute_id': attribute.id,
        })
        template = self.env['product.template'].create({
            'name': 'Variant Internet Service',
            'recurring_invoice': True,
            'attribute_line_ids': [(0, 0, {
                'attribute_id': attribute.id,
                'value_ids': [(6, 0, [speed_50.id, speed_150.id])],
            })],
        })
        variant_50 = template.product_variant_ids.filtered(
            lambda product: speed_50 in product.product_template_attribute_value_ids.product_attribute_value_id
        )
        variant_150 = template.product_variant_ids.filtered(
            lambda product: speed_150 in product.product_template_attribute_value_ids.product_attribute_value_id
        )
        parent = self._create_order([variant_50])
        renewal = self._create_order(
            [variant_50],
            subscription_state='2_renewal',
            renewal_of_id=parent.id,
        )
        self.assertEqual(renewal.service_change_mode, 'no_change')

        renewal.order_line.product_id = variant_150
        self.assertEqual(renewal.service_change_mode, 'no_change')

        result = renewal.action_create_install_task()

        self.assertEqual(renewal.service_change_mode, 'config_only')
        self.assertEqual(renewal.installation_state, 'completed')
        self.assertEqual(renewal.configuration_state, 'to_be_scheduled')
        self.assertEqual(result['params']['title'], 'No Installation Required')
        self.assertFalse(self.env['project.task'].search([
            ('sale_order_id', '=', renewal.id),
        ]))
