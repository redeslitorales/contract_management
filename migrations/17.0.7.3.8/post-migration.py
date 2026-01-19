import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # version is False on fresh install; skip in that case.
    if not version:
        return
