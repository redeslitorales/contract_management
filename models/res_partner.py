import logging
import secrets
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.osv import expression
from odoo.tools import email_normalize

try:
    import dns.resolver
except Exception:
    dns = None

from .email_domain_utils import (
    DEFAULT_BAD_EMAIL_DOMAIN_MAP,
    normalize_email_domain,
    parse_bad_email_domain_map,
)


_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    TOKEN_MAX_AGE_PARAM = "partner_email_verify.token_max_age_hours"
    RESEND_INTERVAL_MINUTES = 10

    email_verified = fields.Boolean(default=False, copy=False)
    email_verify_token = fields.Char(copy=False, index=True)
    email_verify_token_date = fields.Datetime(copy=False)
    email_verify_last_sent = fields.Datetime(copy=False)

    BAD_EMAIL_DOMAIN_PARAM_KEY = 'contract_management.bad_email_domain_map'

    def action_open_change_payment_day_batch_wizard(self):
        self.ensure_one()
        partner = self.commercial_partner_id or self
        active_states = ['3_progress', '4_paused', '5_renewed']
        subscriptions = self.env['sale.order'].search([
            ('partner_id.commercial_partner_id', '=', partner.id),
            ('is_subscription', '=', True),
            ('subscription_state', 'in', active_states),
        ])
        candidate_dates = [d for d in subscriptions.mapped('next_invoice_date') if d]
        base_date = min(candidate_dates) if candidate_dates else fields.Date.context_today(self)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Align Payment Day (All Subs)'),
            'res_model': 'change.payment.date.batch.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_partner_id': partner.id,
                'default_payment_day': base_date.day,
            },
        }

    # Helpers for email domain hygiene
    def _get_bad_email_domain_map(self):
        raw = self.env['ir.config_parameter'].sudo().get_param(self.BAD_EMAIL_DOMAIN_PARAM_KEY, '')
        mapping = parse_bad_email_domain_map(raw)
        return mapping or DEFAULT_BAD_EMAIL_DOMAIN_MAP

    def _normalize_email(self, email):
        email = (email or "").strip()
        return email_normalize(email) or (email if email else False)

    def _mx_check(self, email):
        if not email or "@" not in email:
            _logger.info("Skip MX check: empty or missing @ (email=%s)", email)
            return

        if not dns:
            _logger.warning("Skipping MX check because dnspython is unavailable")
            return

        domain = email.rsplit("@", 1)[-1].lower()
        _logger.warning("MX check entry email=%s domain=%s", email, domain)
        _logger.info("Starting MX check for domain=%s", domain)

        try:
            answers = dns.resolver.resolve(domain, "MX")
            if answers:
                _logger.info("MX found for domain=%s", domain)
                return

            a_answers = dns.resolver.resolve(domain, "A")
            if a_answers:
                _logger.info("No MX; accepting A record fallback for domain=%s", domain)
                return
            raise ValidationError(_("Email domain %(domain)s does not exist or is unreachable. Confirm with the customer and correct the domain (e.g., gmail.com).") % {"domain": domain})

        except dns.resolver.NXDOMAIN:
            _logger.warning("NXDOMAIN during MX check for domain=%s", domain)
            raise ValidationError(_("Email domain %(domain)s does not exist or is unreachable. Confirm with the customer and correct the domain (e.g., gmail.com).") % {"domain": domain})

        except dns.resolver.NoAnswer:
            _logger.warning("NoAnswer during MX check; trying A record for domain=%s", domain)
            try:
                a_answers = dns.resolver.resolve(domain, "A")
                if a_answers:
                    _logger.info("A record present after NoAnswer for domain=%s", domain)
                    return
                raise ValidationError(_("Email domain %(domain)s does not exist or is unreachable. Confirm with the customer and correct the domain (e.g., gmail.com).") % {"domain": domain})
            except dns.resolver.NXDOMAIN:
                _logger.warning("NXDOMAIN after NoAnswer for domain=%s", domain)
                raise ValidationError(_("Email domain %(domain)s does not exist or is unreachable. Confirm with the customer and correct the domain (e.g., gmail.com).") % {"domain": domain})
            except Exception as exc:
                _logger.warning("Fallback A lookup failed for domain=%s error=%s", domain, exc)
                return

        except dns.resolver.Timeout:
            _logger.warning("DNS lookup timed out for domain=%s", domain)
            return

        except Exception as exc:
            _logger.warning("Unexpected MX check failure for domain=%s error=%s", domain, exc)
            return

    def _validate_email(self, email):
        if not email:
            _logger.info("_validate_email skipped: empty email")
            return
        if "@" not in email:
            raise ValidationError(_("Invalid email address. Use user@domain.com."))
        _logger.warning("_validate_email triggered for %s", email)
        _logger.info("_validate_email running MX check for %s", email)
        self._mx_check(email)

    def _new_verify_token(self):
        return secrets.token_urlsafe(32)

    def _set_unverified(self):
        now = fields.Datetime.now()
        self.sudo().write({
            "email_verified": False,
            "email_verify_token": self._new_verify_token(),
            "email_verify_token_date": now,
            "email_verify_last_sent": False,
        })

    def _send_verify_email(self, force=False):
        template = self.env.ref(
            "partner_email_verify.mail_template_partner_email_verify",
            raise_if_not_found=False
        )
        if not template:
            return
        now = fields.Datetime.now()
        for partner in self:
            if partner.email and not partner.email_verified:
                if (
                    not force
                    and partner.email_verify_last_sent
                    and (now - partner.email_verify_last_sent).total_seconds() < self.RESEND_INTERVAL_MINUTES * 60
                ):
                    continue
                template.sudo().send_mail(partner.id, force_send=False, raise_exception=False)
                partner.sudo().write({"email_verify_last_sent": now})

    def action_resend_email_verification(self):
        self.ensure_one()
        if not self.email:
            raise ValidationError(_(
                "Add an email (user@domain.com) and try again."
            ))
        self._set_unverified()
        self._send_verify_email(force=True)
        return True

    def _is_email_token_valid(self, token, max_age_hours=48):
        self.ensure_one()
        if token != self.email_verify_token or not self.email_verify_token_date:
            return False
        hours_param = self.env["ir.config_parameter"].sudo().get_param(self.TOKEN_MAX_AGE_PARAM)
        try:
            max_hours = float(hours_param) if hours_param else max_age_hours
        except Exception:
            max_hours = max_age_hours
        return fields.Datetime.now() <= (
            self.email_verify_token_date + timedelta(hours=max_hours)
        )

    def _validate_email_domain(self, vals):
        if 'email' not in vals:
            return
        email_val = vals.get('email')
        if not email_val:
            return
        corrected = normalize_email_domain(email_val, self._get_bad_email_domain_map())
        if corrected and corrected != email_val:
            raise UserError(_("Domain looks incorrect. Did you mean %(suggested)s? Confirm with the customer and fix the email.") % {"suggested": corrected})

    @api.model_create_multi
    def create(self, vals_list):
        now = fields.Datetime.now()
        _logger.info("ResPartner create hook triggered for %s records", len(vals_list))
        _logger.warning("partner_email_verify create vals_keys=%s", [list(v.keys()) for v in vals_list])
        for vals in vals_list:
            self._validate_email_domain(vals)
            if "email" in vals:
                vals["email"] = self._normalize_email(vals.get("email"))
                self._validate_email(vals.get("email"))
                if vals.get("email"):
                    vals.update({
                        "email_verified": False,
                        "email_verify_token": self._new_verify_token(),
                        "email_verify_token_date": now,
                    })
        partners = super().create(vals_list)
        partners.filtered(lambda p: p.email)._send_verify_email()
        return partners

    def write(self, vals):
        self._validate_email_domain(vals)
        _logger.warning("partner_email_verify write entry ids=%s keys=%s", self.ids, list(vals.keys()))
        email_changed = "email" in vals
        targets = self.browse()

        if email_changed:
            new_email = self._normalize_email(vals.get("email"))
            vals["email"] = new_email
            _logger.warning("partner_email_verify write hook email_changed ids=%s new_email=%s", self.ids, new_email)
            self._validate_email(new_email)
            for partner in self:
                if (partner.email or False) != (new_email or False):
                    targets |= partner
        else:
            _logger.warning("partner_email_verify write skipping email validation (email not in vals) ids=%s", self.ids)

        res = super().write(vals)

        if email_changed and targets:
            targets.filtered(lambda p: p.email)._set_unverified()
            targets._send_verify_email(force=True)

        return res

    @api.model
    def fix_bad_email_domains(self, limit=500, dry_run=False):
        mapping = self._get_bad_email_domain_map()
        if not mapping:
            return {'dry_run': dry_run, 'found': 0, 'to_fix': 0, 'sample': []}

        # Build OR domain to avoid scanning all partners.
        domains = [[('email', 'ilike', '@%s' % bad)] for bad in mapping]
        if not domains:
            return {'dry_run': dry_run, 'found': 0, 'to_fix': 0, 'sample': []}
        search_domain = expression.OR(domains) if len(domains) > 1 else domains[0]

        partners = self.sudo().search(search_domain, limit=limit)
        changes = []
        for partner in partners:
            new_email = normalize_email_domain(partner.email, mapping)
            if new_email and new_email != partner.email:
                changes.append((partner.id, partner.email, new_email))
                if not dry_run:
                    partner.write({'email': new_email})

        if not dry_run and changes:
            _logger.info("Fixed bad email domains for %s partners (limit=%s)", len(changes), limit)

        return {
            'dry_run': dry_run,
            'found': len(partners),
            'to_fix': len(changes),
            'sample': changes[:50],
        }
