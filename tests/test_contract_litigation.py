# -*- coding: utf-8 -*-

import base64
from datetime import timedelta
from html import unescape

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase


class TestContractLitigation(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env["res.partner"].create({
            "name": "Litigation Test Customer",
            "email": "litigation@example.com",
        })
        self.subscription = self.env["sale.order"].create({
            "partner_id": self.partner.id,
        })
        self.subscription.write({"subscription_state": "8_suspend"})
        self.old_suspension_date = fields.Date.context_today(self.subscription) - timedelta(days=100)
        self.subscription.write({"litigation_suspended_on": self.old_suspension_date})
        self.case = self.env["contract.litigation.case"].create({
            "subscription_id": self.subscription.id,
            "suspended_on": self.old_suspension_date,
            "threshold_days": 90,
        })
        self.move = self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": self.partner.id,
            "invoice_date": self.old_suspension_date,
        })

    def _add_claim_line(self, **overrides):
        values = {
            "case_id": self.case.id,
            "move_id": self.move.id,
            "invoice_date": self.old_suspension_date,
            "total_amount": 100.0,
            "residual_amount": 100.0,
            "post_suspension": False,
            "include_in_claim": True,
        }
        values.update(overrides)
        line = self.env["contract.litigation.case.invoice"].create(values)
        # These focused unit tests inject a snapshot line directly rather than
        # constructing a fully posted sale-linked invoice.
        self.case.snapshot_date = False
        return line

    def _complete_checklist(self):
        self.case.write({
            "identity_verified": True,
            "contract_reviewed": True,
            "service_delivery_verified": True,
            "balance_reviewed": True,
            "disputes_cleared": True,
            "address_verified": True,
        })

    def test_suspension_transition_records_date(self):
        subscription = self.env["sale.order"].create({"partner_id": self.partner.id})
        subscription.write({"subscription_state": "8_suspend"})
        self.assertEqual(subscription.litigation_suspended_on, fields.Date.context_today(subscription))
        self.assertEqual(subscription.litigation_suspension_date_source, "state_change")

    def test_whatsapp_falls_back_to_customer_mobile(self):
        self.partner.mobile = "+50370000000"
        subscription = self.env["sale.order"].create({"partner_id": self.partner.id})
        subscription.write({
            "subscription_state": "8_suspend",
            "litigation_suspended_on": self.old_suspension_date,
        })
        case = self.env["contract.litigation.case"].create({
            "subscription_id": subscription.id,
            "suspended_on": self.old_suspension_date,
            "threshold_days": 90,
        })
        self.assertEqual(case.contact_whatsapp, "+50370000000")
        case.contact_whatsapp = False
        self.assertEqual(case._litigation_whatsapp_number(), "+50370000000")

    def test_case_owner_must_be_internal_user(self):
        with self.assertRaises(ValidationError):
            self.case.responsible_id = self.env.ref("base.public_user")

    def test_ready_requires_positive_reviewed_balance_and_checklist(self):
        with self.assertRaises(UserError):
            self.case.action_mark_ready()
        self._add_claim_line()
        self._complete_checklist()
        self.assertEqual(self.case.readiness_status, "ready")
        self.case.action_mark_ready()
        self.assertEqual(self.case.state, "ready")

    def test_post_suspension_items_require_review(self):
        line = self._add_claim_line(
            invoice_date=self.old_suspension_date + timedelta(days=10),
            post_suspension=True,
            include_in_claim=True,
        )
        self._complete_checklist()
        self.assertEqual(self.case.readiness_status, "remediation")
        self.case.post_suspension_reviewed = True
        self.assertEqual(self.case.readiness_status, "ready")
        self.case.action_mark_ready()
        with self.assertRaises(UserError):
            line.include_in_claim = False

    def test_legal_hold_blocks_escalation(self):
        self._add_claim_line()
        self._complete_checklist()
        self.subscription.write({
            "litigation_hold": True,
            "litigation_hold_reason": "Customer dispute under review",
        })
        self.assertEqual(self.case.readiness_status, "blocked")
        with self.assertRaises(UserError):
            self.case.action_mark_ready()

    def test_refresh_is_locked_after_ready(self):
        self._add_claim_line()
        self._complete_checklist()
        self.case.action_mark_ready()
        with self.assertRaises(UserError):
            self.case.action_refresh_snapshot()

    def test_contract_requires_signed_pagare_verification(self):
        contract = self.env["contract.management"].create({
            "subscription_id": self.subscription.id,
            "state": "active",
            "contract_value": 500.0,
            "contract_file": base64.b64encode(b"%PDF-1.4 signed contract with pagare"),
            "contract_filename": "signed_contract.pdf",
        })
        self._add_claim_line()
        self._complete_checklist()

        self.assertTrue(self.case.has_contract)
        self.assertTrue(self.case.has_contract_evidence)
        self.assertEqual(self.case.applicable_contract_id, contract)
        self.assertEqual(self.case.pagare_face_value, 0.0)
        self.assertEqual(self.case.readiness_status, "remediation")
        with self.assertRaises(UserError):
            self.case.action_print_package()

        self.case.pagare_verified = True
        self.assertEqual(self.case.readiness_status, "ready")

        template = self.env.ref("contract_management.mail_template_litigation_initial_notice")
        self.assertEqual(template.email_from, "Cabal <DoNotReply@cabal.sv>")
        self.assertEqual(template.reply_to, "legal@cabal.sv")
        rendered = unescape(str(template._render_field("body_html", [self.case.id])[self.case.id]))
        self.assertIn("una de las siguientes alternativas", rendered)
        self.assertIn("REGULARIZAR LA CUENTA", rendered)
        self.assertIn("TERMINACIÓN ANTICIPADA", rendered)
        self.assertIn("MONTO AJUSTADO DEL PAGARÉ", rendered)
        self.assertIn("$500.00", rendered)
        self.assertNotIn("valor nominal", rendered)
        self.assertIn("agencia de cobros", rendered)
        self.assertIn("tribunales competentes", rendered)

        components = self.case._litigation_whatsapp_components(
            "https://example.test/notice.pdf", "notice.pdf"
        )
        self.assertEqual(
            self.case._litigation_whatsapp_template_name(),
            "litigation_initial_notice_contract_v2",
        )
        self.assertEqual(components[0]["parameters"][0]["type"], "document")
        self.assertEqual(len(components[1]["parameters"]), 4)
        sms_text = self.case._litigation_initial_sms_text(
            fields.Date.context_today(self.case) + timedelta(days=10)
        )
        self.assertIn("Saldo", sms_text)
        self.assertIn("cobros y/o tribunales", sms_text)
        self.assertNotIn("nominal", sms_text.lower())
        self.assertLessEqual(len(sms_text), 160)

    def test_no_contract_notice_demands_payment_only(self):
        self._add_claim_line()
        self._complete_checklist()
        self.assertFalse(self.case.has_contract)

        template = self.env.ref("contract_management.mail_template_litigation_initial_notice")
        rendered_subject = unescape(str(template._render_field("subject", [self.case.id])[self.case.id]))
        rendered_body = unescape(str(template._render_field("body_html", [self.case.id])[self.case.id]))
        self.assertIn("Requerimiento formal de pago", rendered_subject)
        self.assertIn("Le requerimos cancelar", rendered_body)
        self.assertIn("íntegramente el saldo", rendered_body)
        self.assertIn("agencia de cobros", rendered_body)
        self.assertIn("tribunales competentes", rendered_body)
        self.assertNotIn("una de las siguientes alternativas", rendered_body)
        self.assertNotIn("TERMINACIÓN ANTICIPADA", rendered_body)

        components = self.case._litigation_whatsapp_components(
            "https://example.test/notice.pdf", "notice.pdf"
        )
        self.assertEqual(
            self.case._litigation_whatsapp_template_name(),
            "litigation_initial_notice_balance",
        )
        self.assertEqual(components[0]["parameters"][0]["type"], "document")
        self.assertEqual(len(components[1]["parameters"]), 4)

    def test_contract_record_without_evidence_uses_balance_only_enforcement(self):
        contract = self.env["contract.management"].create({
            "subscription_id": self.subscription.id,
            "state": "active",
            "contract_value": 500.0,
        })
        self._add_claim_line()
        self._complete_checklist()

        self.assertTrue(self.case.has_contract)
        self.assertFalse(self.case.has_contract_evidence)
        self.assertEqual(self.case.applicable_contract_id, contract)
        self.assertEqual(self.case.readiness_status, "ready")
        self.case._ensure_pagare_enforcement_ready()

        template = self.env.ref("contract_management.mail_template_litigation_initial_notice")
        rendered_subject = unescape(str(template._render_field("subject", [self.case.id])[self.case.id]))
        rendered_body = unescape(str(template._render_field("body_html", [self.case.id])[self.case.id]))
        self.assertIn("Requerimiento formal de pago", rendered_subject)
        self.assertIn("Le requerimos cancelar", rendered_body)
        self.assertNotIn("TERMINACIÃ“N ANTICIPADA", rendered_body)
        self.assertEqual(
            self.case._litigation_whatsapp_template_name(),
            "litigation_initial_notice_balance",
        )

    def test_initial_notice_omits_invoice_when_hacienda_pdf_is_missing(self):
        self._add_claim_line()

        parts, missing_invoices = self.case._litigation_overdue_dte_pdf_parts(
            return_missing=True
        )

        self.assertEqual(parts, [])
        self.assertEqual(missing_invoices, [self.move.display_name])
        warning_messages = self.case.message_ids.filtered(
            lambda message: "Hacienda DTE PDF" in (message.body or "")
        )
        self.assertTrue(warning_messages)

    def test_initial_notice_pdf_body_preserves_utf8_metadata(self):
        report = self.env.ref("contract_management.action_report_litigation_initial_notice")
        html, _ = report.with_context(lang="es_419")._render_qweb_html(
            report.report_name, [self.case.id]
        )
        bodies, _res_ids, _header, _footer, _paperformat = report._prepare_html(
            html, report_model=report.model
        )

        self.assertEqual(len(bodies), 1)
        self.assertIn('<meta charset="utf-8"', str(bodies[0]).lower())
        self.assertIn("Fecha límite", str(bodies[0]))
