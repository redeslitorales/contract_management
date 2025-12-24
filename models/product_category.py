from odoo import models, fields

class ProductContract(models.Model):
    _inherit = 'product.category'

    contract_template = fields.Many2one('ir.actions.report', string='Contract Template', domain=[('name', 'ilike', 'Contract')])