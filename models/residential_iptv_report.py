from odoo import fields, models, tools


class ResidentialInternetIptvReport(models.Model):
    _name = 'residential.internet.iptv.report'
    _description = '$25 Residential Internet Customers with IPTV'
    _auto = False
    _order = 'partner_id'

    partner_id = fields.Many2one('res.partner', string='Customer', readonly=True)
    phone = fields.Char(related='partner_id.phone', readonly=True)
    mobile = fields.Char(related='partner_id.mobile', readonly=True)
    email = fields.Char(related='partner_id.email', readonly=True)
    internet_subscriptions = fields.Char(string='Residential Internet Subscriptions', readonly=True)
    iptv_subscriptions = fields.Char(string='IPTV Subscriptions', readonly=True)
    internet_products = fields.Char(string='$25 Internet Plans', readonly=True)
    iptv_products = fields.Char(string='IPTV Plans', readonly=True)
    internet_service_count = fields.Integer(string='$25 Internet Services', readonly=True)
    iptv_account_count = fields.Integer(string='IPTV Accounts', readonly=True)
    internet_monthly_amount = fields.Float(
        string='Internet Monthly Amount',
        digits='Product Price',
        readonly=True,
    )
    iptv_monthly_amount = fields.Float(
        string='IPTV Monthly Amount',
        digits='Product Price',
        readonly=True,
    )
    total_monthly_payment = fields.Float(
        string='Total Monthly Payment',
        digits='Product Price',
        readonly=True,
        help="Tax-inclusive total of all recurring lines on the customer's current subscriptions.",
    )
    same_subscription = fields.Boolean(string='Services on Same Subscription', readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(
            """
            CREATE OR REPLACE VIEW residential_internet_iptv_report AS (
                WITH residential_lines AS (
                    SELECT
                        commercial_partner.id AS partner_id,
                        subscription.id AS subscription_id,
                        COALESCE(subscription.cabal_sequence, subscription.name) AS subscription_name,
                        line.product_uom_qty AS service_quantity,
                        line.price_total AS monthly_amount,
                        COALESCE(
                            product_template.name->>'en_US',
                            product_template.name->>'es_419',
                            product_template.name->>'es_ES',
                            product_template.name::text
                        ) AS product_name
                    FROM sale_order_line AS line
                    JOIN sale_order AS subscription
                      ON subscription.id = line.order_id
                    JOIN res_partner AS subscription_partner
                      ON subscription_partner.id = subscription.partner_id
                    JOIN res_partner AS commercial_partner
                      ON commercial_partner.id = subscription_partner.commercial_partner_id
                    JOIN product_product AS product
                      ON product.id = line.product_id
                    JOIN product_template
                      ON product_template.id = product.product_tmpl_id
                    JOIN product_category AS category
                      ON category.id = product_template.categ_id
                    JOIN ir_model_data AS residential_template
                      ON residential_template.model = 'ir.actions.report'
                     AND residential_template.res_id = category.contract_template
                     AND residential_template.module = 'contract_management'
                     AND residential_template.name = 'action_report_contract'
                    WHERE subscription.is_subscription IS TRUE
                      AND subscription.subscription_state IN ('3_progress', '4_paused', '8_suspend')
                      AND line.display_type IS NULL
                      AND product_template.recurring_invoice IS TRUE
                      AND ROUND(line.price_unit::numeric, 2) = 25.00
                ),
                iptv_lines AS (
                    SELECT
                        commercial_partner.id AS partner_id,
                        subscription.id AS subscription_id,
                        COALESCE(subscription.cabal_sequence, subscription.name) AS subscription_name,
                        line.product_uom_qty AS account_quantity,
                        line.price_total AS monthly_amount,
                        COALESCE(
                            product_template.name->>'en_US',
                            product_template.name->>'es_419',
                            product_template.name->>'es_ES',
                            product_template.name::text
                        ) AS product_name
                    FROM sale_order_line AS line
                    JOIN sale_order AS subscription
                      ON subscription.id = line.order_id
                    JOIN res_partner AS subscription_partner
                      ON subscription_partner.id = subscription.partner_id
                    JOIN res_partner AS commercial_partner
                      ON commercial_partner.id = subscription_partner.commercial_partner_id
                    JOIN product_product AS product
                      ON product.id = line.product_id
                    JOIN product_template
                      ON product_template.id = product.product_tmpl_id
                    JOIN product_category AS category
                      ON category.id = product_template.categ_id
                    JOIN ir_model_data AS iptv_template
                      ON iptv_template.model = 'ir.actions.report'
                     AND iptv_template.res_id = category.contract_template
                     AND iptv_template.module = 'contract_management'
                     AND iptv_template.name = 'action_iptv_contract'
                    WHERE subscription.is_subscription IS TRUE
                      AND subscription.subscription_state IN ('3_progress', '4_paused', '8_suspend')
                      AND line.display_type IS NULL
                      AND product_template.recurring_invoice IS TRUE
                ),
                residential_customers AS (
                    SELECT
                        partner_id,
                        STRING_AGG(DISTINCT subscription_name, ', ' ORDER BY subscription_name) AS subscriptions,
                        STRING_AGG(DISTINCT product_name, ', ' ORDER BY product_name) AS products,
                        ROUND(SUM(service_quantity))::integer AS service_count,
                        SUM(monthly_amount) AS monthly_amount
                    FROM residential_lines
                    GROUP BY partner_id
                ),
                iptv_customers AS (
                    SELECT
                        partner_id,
                        STRING_AGG(DISTINCT subscription_name, ', ' ORDER BY subscription_name) AS subscriptions,
                        STRING_AGG(DISTINCT product_name, ', ' ORDER BY product_name) AS products,
                        ROUND(SUM(account_quantity))::integer AS account_count,
                        SUM(monthly_amount) AS monthly_amount
                    FROM iptv_lines
                    GROUP BY partner_id
                ),
                customer_monthly_totals AS (
                    SELECT
                        commercial_partner.id AS partner_id,
                        SUM(line.price_total) AS total_monthly_payment
                    FROM sale_order_line AS line
                    JOIN sale_order AS subscription
                      ON subscription.id = line.order_id
                    JOIN res_partner AS subscription_partner
                      ON subscription_partner.id = subscription.partner_id
                    JOIN res_partner AS commercial_partner
                      ON commercial_partner.id = subscription_partner.commercial_partner_id
                    JOIN product_product AS product
                      ON product.id = line.product_id
                    JOIN product_template
                      ON product_template.id = product.product_tmpl_id
                    WHERE subscription.is_subscription IS TRUE
                      AND subscription.subscription_state IN ('3_progress', '4_paused', '8_suspend')
                      AND line.display_type IS NULL
                      AND product_template.recurring_invoice IS TRUE
                    GROUP BY commercial_partner.id
                )
                SELECT
                    residential.partner_id AS id,
                    residential.partner_id,
                    residential.subscriptions AS internet_subscriptions,
                    iptv.subscriptions AS iptv_subscriptions,
                    residential.products AS internet_products,
                    iptv.products AS iptv_products,
                    residential.service_count AS internet_service_count,
                    iptv.account_count AS iptv_account_count,
                    residential.monthly_amount AS internet_monthly_amount,
                    iptv.monthly_amount AS iptv_monthly_amount,
                    monthly_totals.total_monthly_payment,
                    EXISTS (
                        SELECT 1
                        FROM residential_lines AS residential_line
                        JOIN iptv_lines AS iptv_line
                          ON iptv_line.partner_id = residential_line.partner_id
                         AND iptv_line.subscription_id = residential_line.subscription_id
                        WHERE residential_line.partner_id = residential.partner_id
                    ) AS same_subscription
                FROM residential_customers AS residential
                JOIN iptv_customers AS iptv
                  ON iptv.partner_id = residential.partner_id
                JOIN customer_monthly_totals AS monthly_totals
                  ON monthly_totals.partner_id = residential.partner_id
            )
            """
        )
