# -*- coding: utf-8 -*-
import unittest
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class TestInstallationTaskSelection(TransactionCase):
    """Installation task matching must not depend on order-line position."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not cls.env.registry.get('fsm.task.type'):
            raise unittest.SkipTest('FSM guided intake is not installed')
        cls.partner = cls.env['res.partner'].create({
            'name': 'Installation Task Selection Customer',
        })
        cls.equipment_category = cls.env['product.category'].create({
            'name': 'Test Access Points',
        })
        cls.service_category = cls.env['product.category'].create({
            'name': 'Test Business Internet',
        })
        cls.equipment_product = cls.env['product.product'].create({
            'name': 'Test Access Point',
            'categ_id': cls.equipment_category.id,
        })
        cls.service_product = cls.env['product.product'].create({
            'name': 'Test Business Internet Service',
            'categ_id': cls.service_category.id,
        })

        project_values = {
            'name': 'Test Installation Project',
            'company_id': cls.env.company.id,
        }
        if 'is_fsm' in cls.env['project.project']._fields:
            project_values['is_fsm'] = True
        cls.installation_project = cls.env['project.project'].create(project_values)
        cls.installation_task_type = cls.env['fsm.task.type'].create({
            'name': 'Test Business Internet Installation',
            'project_id': cls.installation_project.id,
            'is_installation': True,
            'subscription_category_ids': [(6, 0, [cls.service_category.id])],
        })

    def test_matches_later_service_category_when_equipment_is_first(self):
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'order_line': [
                (0, 0, {
                    'sequence': 10,
                    'product_id': self.equipment_product.id,
                    'product_uom_qty': 1,
                }),
                (0, 0, {
                    'sequence': 20,
                    'product_id': self.service_product.id,
                    'product_uom_qty': 1,
                }),
            ],
        })

        result = order.action_create_install_task()

        task = self.env['project.task'].search([
            ('sale_order_id', '=', order.id),
        ])
        self.assertEqual(len(task), 1)
        self.assertEqual(task.fsm_task_type_id, self.installation_task_type)
        self.assertEqual(result['params']['title'], 'Installation Task Created')

    def test_scheduled_task_accepts_fsm_subscription_link(self):
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'installation_state': 'to_be_scheduled',
            'configuration_state': 'to_be_scheduled',
        })

        self.env['project.task'].with_context(
            fsm_skip_auto_stage=True,
        ).create({
            'name': 'Installation linked through FSM subscription',
            'project_id': self.installation_project.id,
            'partner_id': self.partner.id,
            'fsm_task_type_id': self.installation_task_type.id,
            'fsm_subscription_id': order.id,
            'planned_date_begin': fields.Datetime.now() + timedelta(days=1),
        })

        self.assertEqual(order.installation_state, 'scheduled')
        self.assertEqual(order.configuration_state, 'scheduled')

    def test_plain_internet_order_never_selects_iptv_task_type(self):
        self.env['fsm.task.type'].create({
            'name': 'Install IPTV - Competing Alphabetical Match',
            'project_id': self.installation_project.id,
            'is_installation': True,
            'subscription_category_ids': [(6, 0, [self.service_category.id])],
        })
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'order_line': [(0, 0, {
                'product_id': self.service_product.id,
                'product_uom_qty': 1,
            })],
        })

        order.action_create_install_task()

        task = self.env['project.task'].search([
            ('sale_order_id', '=', order.id),
        ])
        self.assertEqual(task.fsm_task_type_id, self.installation_task_type)
