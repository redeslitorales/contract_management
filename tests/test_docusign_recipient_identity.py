# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestDocuSignRecipientIdentity(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'Original Envelope Name',
            'email': 'signer@example.com',
        })
        self.connector = self.env['docusign.connector'].create({
            'responsible_id': self.env.user.id,
        })
        self.line = self.env['docusign.connector.lines'].create({
            'partner_id': self.partner.id,
            'record_id': self.connector.id,
            'recipient_name': self.partner.name,
            'recipient_email': self.partner.email,
            'status': 'sent',
            'envelope_id': 'test-envelope-id',
            'send_status': True,
        })

    def test_partner_rename_does_not_change_envelope_identity(self):
        self.partner.name = 'Corrected Customer Name'

        self.assertEqual(self.line._get_recipient_name(), 'Original Envelope Name')
        self.assertEqual(self.line._get_recipient_email(), 'signer@example.com')

    def test_legacy_line_without_snapshot_falls_back_to_current_partner_name(self):
        self.line.recipient_name = False
        self.partner.name = 'Corrected Customer Name'

        self.assertEqual(self.line._get_recipient_name(), 'Corrected Customer Name')

    def test_resend_keeps_previous_unexpired_link_valid(self):
        first_token, _first_url = self.line.generate_magic_link()
        second_token, _second_url = self.line.generate_magic_link()

        first_line, first_error = self.line.resolve_magic_token(first_token)
        second_line, second_error = self.line.resolve_magic_token(second_token)

        self.assertEqual(first_line, self.line)
        self.assertFalse(first_error)
        self.assertEqual(second_line, self.line)
        self.assertFalse(second_error)

    def test_completion_invalidates_all_links(self):
        first_token, _first_url = self.line.generate_magic_link()
        second_token, _second_url = self.line.generate_magic_link()

        self.line.consume_magic_token()

        self.assertEqual(self.line.resolve_magic_token(first_token), (False, 'used'))
        self.assertEqual(self.line.resolve_magic_token(second_token), (False, 'used'))

    def test_unsigned_envelope_document_is_replaced(self):
        old_attachment = self.env['ir.attachment'].create({
            'name': 'old.pdf',
            'datas': 'b2xk',
            'mimetype': 'application/pdf',
        })
        new_attachment = self.env['ir.attachment'].create({
            'name': 'new.pdf',
            'datas': 'bmV3',
            'mimetype': 'application/pdf',
        })
        self.connector.attachment_ids = [(6, 0, [old_attachment.id])]

        helper_path = (
            'odoo.addons.contract_management.models.docusign_connector.docu_client'
        )
        with patch(
            f'{helper_path}.get_envelope_details',
            return_value={
                'status': 'sent',
                'envelopeDocuments': [{'documentId': '7'}],
            },
        ), patch(f'{helper_path}.replace_envelope_document') as replace_document:
            self.connector.replace_unsigned_envelope_document(new_attachment)

        replace_document.assert_called_once()
        call_args = replace_document.call_args.args
        self.assertEqual(call_args[2], 'test-envelope-id')
        self.assertEqual(call_args[3], '7')
        self.assertEqual(call_args[4], 'new.pdf')
        self.assertEqual(self.connector.attachment_ids, new_attachment)
        self.assertEqual(self.line.un_signed_attachment_ids, new_attachment)

    def test_signed_envelope_document_cannot_be_replaced(self):
        attachment = self.env['ir.attachment'].create({
            'name': 'new.pdf',
            'datas': 'bmV3',
            'mimetype': 'application/pdf',
        })
        self.line.sign_status = True

        with self.assertRaises(ValidationError):
            self.connector.replace_unsigned_envelope_document(attachment)
