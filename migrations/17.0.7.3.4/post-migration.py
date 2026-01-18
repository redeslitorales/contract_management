import logging

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
# constraints (draft for quotes, progress for confirmed orders).
STATE_FIXES = [
    (
        "1a_pending -> draft/progress + pending customer signature",
        """
        UPDATE sale_order so
           SET subscription_state = CASE WHEN so.state IN ('sale', 'done') THEN '9_pending' ELSE '1_draft' END,
               contract_state = 'pending_customer_signature',
               next_invoice_date = CASE
                   WHEN so.state IN ('sale', 'done')
                        AND NOT EXISTS (
                            SELECT 1 FROM sale_order_invoice_rel rel WHERE rel.order_id = so.id
                        )
                   THEN '2030-12-31'
                   ELSE so.next_invoice_date
               END
         WHERE so.subscription_state = '1a_pending'
           AND COALESCE(so.is_subscription, FALSE) = TRUE
        """,
    ),
    (
        "1d_internal -> draft/progress + pending cabal signature",
        """
        UPDATE sale_order so
           SET subscription_state = CASE WHEN so.state IN ('sale', 'done') THEN '9_pending' ELSE '1_draft' END,
               contract_state = 'pending_cabal_signature',
               next_invoice_date = CASE
                   WHEN so.state IN ('sale', 'done')
                        AND NOT EXISTS (
                            SELECT 1 FROM sale_order_invoice_rel rel WHERE rel.order_id = so.id
                        )
                   THEN '2030-12-31'
                   ELSE so.next_invoice_date
               END
         WHERE so.subscription_state = '1d_internal'
           AND COALESCE(so.is_subscription, FALSE) = TRUE
        """,
    ),
    (
        "1e_confirm -> draft/progress + quote_confirmed",
        """
        UPDATE sale_order so
           SET subscription_state = CASE WHEN so.state IN ('sale', 'done') THEN '9_pending' ELSE '1_draft' END,
               quote_confirmed = TRUE,
               next_invoice_date = CASE
                   WHEN so.state IN ('sale', 'done')
                        AND NOT EXISTS (
                            SELECT 1 FROM sale_order_invoice_rel rel WHERE rel.order_id = so.id
                        )
                   THEN '2030-12-31'
                   ELSE so.next_invoice_date
               END
         WHERE so.subscription_state = '1e_confirm'
           AND COALESCE(so.is_subscription, FALSE) = TRUE
        """,
    ),
    (
        "1c_ncontract -> draft/progress + pending_contract",
        """
        UPDATE sale_order so
           SET subscription_state = CASE WHEN so.state IN ('sale', 'done') THEN '9_pending' ELSE '1_draft' END,
               contract_state = 'pending_contract',
               next_invoice_date = CASE
                   WHEN so.state IN ('sale', 'done')
                        AND NOT EXISTS (
                            SELECT 1 FROM sale_order_invoice_rel rel WHERE rel.order_id = so.id
                        )
                   THEN '2030-12-31'
                   ELSE so.next_invoice_date
               END
         WHERE so.subscription_state = '1c_ncontract'
           AND COALESCE(so.is_subscription, FALSE) = TRUE
        """,
    ),
    (
        "1b_install -> draft/progress + installation scheduled",
        """
        UPDATE sale_order so
           SET subscription_state = CASE WHEN so.state IN ('sale', 'done') THEN '9_pending' ELSE '1_draft' END,
               installation_state = 'scheduled',
               next_invoice_date = CASE
                   WHEN so.state IN ('sale', 'done')
                        AND NOT EXISTS (
                            SELECT 1 FROM sale_order_invoice_rel rel WHERE rel.order_id = so.id
                        )
                   THEN '2030-12-31'
                   ELSE so.next_invoice_date
               END
         WHERE so.subscription_state = '1b_install'
           AND COALESCE(so.is_subscription, FALSE) = TRUE
        """,
    ),
    (
        "1e_schedule -> draft/progress + installation to be scheduled",
        """
        UPDATE sale_order so
           SET subscription_state = CASE WHEN so.state IN ('sale', 'done') THEN '9_pending' ELSE '1_draft' END,
               installation_state = 'to_be_scheduled',
               next_invoice_date = CASE
                   WHEN so.state IN ('sale', 'done')
                        AND NOT EXISTS (
                            SELECT 1 FROM sale_order_invoice_rel rel WHERE rel.order_id = so.id
                        )
                   THEN '2030-12-31'
                   ELSE so.next_invoice_date
               END
         WHERE so.subscription_state = '1e_schedule'
           AND COALESCE(so.is_subscription, FALSE) = TRUE
        """,
    ),
]


def migrate(cr, version):
    """Normalize legacy subscription states after removing intermediate statuses."""
    if not version:
        return

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

    for label, query in STATE_FIXES:
        cr.execute(query)
        _logger.info("[contract_management][migration] %s: %s rows updated", label, cr.rowcount)

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

    _logger.warning("[contract_management][migration] END post-migration 17.0.7.3.4")
