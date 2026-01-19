import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

# Cleanup for legacy states on non-subscription orders (keep constraint happy).
LEGACY_STATES = (
    "1a_pending",
    "1d_internal",
    "1e_confirm",
    "1c_ncontract",
    "1b_install",
    "1e_schedule",
)


# Maps old subscription_state values to new states while respecting coherence
# constraints. All legacy values move to draft + sent while retaining their
# specific contract/installation metadata.
STATE_FIXES = [
    {
        "label": "1a_pending -> draft + pending customer signature",
        "legacy_state": "1a_pending",
        "contract_state": "pending_customer_signature",
        "installation_state": None,
        "quote_confirmed": None,
        "set_next_invoice_date": True,
    },
    {
        "label": "1d_internal -> draft + pending cabal signature",
        "legacy_state": "1d_internal",
        "contract_state": "pending_cabal_signature",
        "installation_state": None,
        "quote_confirmed": None,
        "set_next_invoice_date": True,
    },
    {
        "label": "1e_confirm -> draft + quote_confirmed",
        "legacy_state": "1e_confirm",
        "contract_state": None,
        "installation_state": None,
        "quote_confirmed": True,
        "set_next_invoice_date": True,
    },
    {
        "label": "1c_ncontract -> draft + pending_contract",
        "legacy_state": "1c_ncontract",
        "contract_state": "pending_contract",
        "installation_state": None,
        "quote_confirmed": None,
        "set_next_invoice_date": True,
    },
    {
        "label": "1b_install -> draft + installation scheduled",
        "legacy_state": "1b_install",
        "contract_state": None,
        "installation_state": "scheduled",
        "quote_confirmed": None,
        "set_next_invoice_date": True,
    },
    {
        "label": "1e_schedule -> draft + installation to be scheduled",
        "legacy_state": "1e_schedule",
        "contract_state": None,
        "installation_state": "to_be_scheduled",
        "quote_confirmed": None,
        "set_next_invoice_date": True,
    },
]


def _apply_state_fix(env, fix):
    """Move a legacy subscription_state to draft+sent and log chatter."""

    cr = env.cr
    cr.execute(
        """
        SELECT id
          FROM sale_order
         WHERE subscription_state = %s
           AND COALESCE(is_subscription, FALSE) = TRUE
        """,
        (fix["legacy_state"],),
    )
    order_ids = [row[0] for row in cr.fetchall()]
    if not order_ids:
        _logger.info(
            "[contract_management][migration] %s: no rows to update", fix["label"]
        )
        return

    cr.execute(
        """
        UPDATE sale_order AS so
           SET subscription_state = '1_draft',
               state = 'sent',
               contract_state = CASE WHEN %(contract_state)s IS NOT NULL THEN %(contract_state)s ELSE contract_state END,
               installation_state = CASE WHEN %(installation_state)s IS NOT NULL THEN %(installation_state)s ELSE installation_state END,
               quote_confirmed = CASE WHEN %(quote_confirmed)s IS NOT NULL THEN %(quote_confirmed)s ELSE quote_confirmed END,
               next_invoice_date = CASE
                   WHEN %(set_next_invoice_date)s
                        AND so.state IN ('sale', 'done')
                        AND NOT EXISTS (
                            SELECT 1 FROM sale_order_invoice_rel rel WHERE rel.order_id = so.id
                        )
                   THEN '2030-12-31'
                   ELSE so.next_invoice_date
               END
         WHERE so.id = ANY(%(order_ids)s)
        """,
        {
            "contract_state": fix["contract_state"],
            "installation_state": fix["installation_state"],
            "quote_confirmed": fix["quote_confirmed"],
            "set_next_invoice_date": fix["set_next_invoice_date"],
            "order_ids": order_ids,
        },
    )

    env["sale.order"].browse(order_ids).message_post(
        body=(
            f"Subscription state migrated from {fix['legacy_state']} to 1_draft; "
            f"order state set to sent (post-migration 17.0.7.3.4)."
        )
    )

    _logger.info(
        "[contract_management][migration] %s: %s rows updated", fix["label"], cr.rowcount
    )


def migrate(cr, version):
    """Normalize legacy subscription states after removing intermediate statuses."""
    env = api.Environment(cr, SUPERUSER_ID, {})

    _logger.warning("[contract_management][migration] START post-migration 17.0.7.3.4")

    # Non-subscription orders should not carry subscription_state values.
    cr.execute(
        """
        UPDATE sale_order
           SET subscription_state = NULL
         WHERE subscription_state IN %s
           AND COALESCE(is_subscription, FALSE) = FALSE
        """,
        (LEGACY_STATES,),
    )
    _logger.info(
        "[contract_management][migration] Cleared legacy states on non-subscriptions: %s rows updated",
        cr.rowcount,
    )

    for fix in STATE_FIXES:
        _apply_state_fix(env, fix)

    # Align contract_state from contract.management when it has a terminal/active status.
    cr.execute(
        """
        UPDATE sale_order AS so
           SET contract_state = CASE
                   WHEN cm.state = 'renewal_due' THEN 'active'
                   ELSE cm.state
               END
          FROM contract_management AS cm
         WHERE cm.subscription_id = so.id
           AND cm.state IN ('active', 'expired', 'terminated', 'renewal_due')
           AND COALESCE(so.is_subscription, FALSE) = TRUE
        """
    )
    _logger.warning(
        "[contract_management][migration] Synced contract_state from contract.management state rows=%s",
        cr.rowcount,
    )

    # For remaining contracts, align contract_state to DocuSign status hints.
    cr.execute(
        """
        UPDATE sale_order AS so
           SET contract_state = CASE
                   WHEN cm.docusign_status IN ('new', 'open') THEN 'pending_contract'
                   WHEN cm.docusign_status = 'sent' THEN 'pending_customer_signature'
                   WHEN cm.docusign_status = 'customer' THEN 'pending_cabal_signature'
                   WHEN cm.docusign_status = 'completed' THEN 'active'
                   ELSE so.contract_state
               END
          FROM contract_management AS cm
         WHERE cm.subscription_id = so.id
           AND COALESCE(so.is_subscription, FALSE) = TRUE
           AND (cm.state NOT IN ('active', 'expired', 'terminated', 'renewal_due') OR cm.state IS NULL)
           AND cm.docusign_status IN ('new', 'open', 'sent', 'customer', 'completed')
        """
    )
    _logger.warning(
        "[contract_management][migration] Synced contract_state from docusign_status rows=%s",
        cr.rowcount,
    )

    # Internet service state alignment from CPE status.
    cr.execute(
        """
        UPDATE sale_order AS so
           SET internet_service_state = 'active'
          FROM account_asset AS aa
         WHERE so.cpe_unit_asset = aa.id
           AND aa.onu_state = 'enabled'
           AND COALESCE(so.is_subscription, FALSE) = TRUE
        """
    )
    _logger.warning(
        "[contract_management][migration] Internet service set to active from enabled CPE rows=%s",
        cr.rowcount,
    )

    cr.execute(
        """
        UPDATE sale_order AS so
           SET internet_service_state = 'suspended'
          FROM account_asset AS aa
         WHERE so.cpe_unit_asset = aa.id
           AND aa.onu_state = 'disabled'
           AND so.subscription_state = '8_suspend'
           AND COALESCE(so.is_subscription, FALSE) = TRUE
        """
    )
    _logger.warning(
        "[contract_management][migration] Internet service set to suspended from disabled CPE + suspended subscription rows=%s",
        cr.rowcount,
    )

    cr.execute(
        """
        UPDATE sale_order AS so
           SET internet_service_state = 'paused'
          FROM account_asset AS aa
         WHERE so.cpe_unit_asset = aa.id
           AND aa.onu_state = 'disabled'
           AND so.subscription_state = '4_paused'
           AND COALESCE(so.is_subscription, FALSE) = TRUE
        """
    )
    _logger.warning(
        "[contract_management][migration] Internet service set to paused from disabled CPE + paused subscription rows=%s",
        cr.rowcount,
    )

    cr.execute(
        """
        UPDATE sale_order AS so
           SET internet_service_state = 'not_active'
         WHERE COALESCE(so.cpe_unit, 0) = 0
           AND COALESCE(so.is_subscription, FALSE) = TRUE
        """
    )
    _logger.warning(
        "[contract_management][migration] Internet service set to not_active when no CPE assigned rows=%s",
        cr.rowcount,
    )

    # Compute install/config states for all subscriptions: complete when in progress, otherwise to be scheduled.
    cr.execute(
        """
        UPDATE sale_order
           SET installation_state = CASE
                   WHEN subscription_state = '3_progress' THEN 'completed'
                   ELSE 'to_be_scheduled'
               END,
               configuration_state = CASE
                   WHEN subscription_state = '3_progress' THEN 'completed'
                   ELSE 'to_be_scheduled'
               END
         WHERE COALESCE(is_subscription, FALSE) = TRUE
        """
    )
    _logger.warning(
        "[contract_management][migration] Computed installation/configuration state from subscription_state rows=%s",
        cr.rowcount,
    )

    _logger.warning("[contract_management][migration] END post-migration 17.0.7.3.4")
