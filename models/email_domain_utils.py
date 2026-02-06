# -*- coding: utf-8 -*-
"""Utilities to normalize and validate email domains."""

from __future__ import annotations

from typing import Dict, Optional

DEFAULT_BAD_EMAIL_DOMAIN_MAP: Dict[str, str] = {
    "gamil.com": "gmail.com",
    "gmial.com": "gmail.com",
    "gmai.com": "gmail.com",
    "gmail.con": "gmail.com",
    "gmail.co": "gmail.com",
    "gmail.cm": "gmail.com",
    "gmail.comm": "gmail.com",
    "gmal.com": "gmail.com",
    "gnail.com": "gmail.com",
    "gmaill.com": "gmail.com",
    "gmailcom": "gmail.com",  # missing dot
    "gmail.cmo": "gmail.com",
}


def parse_bad_email_domain_map(raw: str) -> Dict[str, str]:
    """
    Parse a newline-separated mapping list into a dict.

    Accepts separators "->", ":" or "," per line, and lowercases domains.
    """
    if not raw:
        return {}
    mapping: Dict[str, str] = {}
    for line in raw.splitlines():
        item = (line or "").strip()
        if not item or item.startswith("#"):
            continue
        bad: Optional[str] = None
        good: Optional[str] = None
        for sep in ("->", ":", ","):
            if sep in item:
                left, right = item.split(sep, 1)
                bad = (left or "").strip().lower()
                good = (right or "").strip().lower()
                break
        if not bad or not good:
            continue
        mapping[bad] = good
    return mapping


def normalize_email_domain(email: str, mapping: Dict[str, str]) -> Optional[str]:
    """Return corrected email if the domain matches the mapping; otherwise None."""
    if not email:
        return None
    trimmed = email.strip()
    if "@" not in trimmed:
        return None
    local, domain = trimmed.rsplit("@", 1)
    domain_norm = domain.strip().lower()
    if domain_norm not in mapping:
        return None
    return f"{local}@{mapping[domain_norm]}"


def format_default_bad_email_domain_map() -> str:
    """Return the default mapping as a human-editable string."""
    return "\n".join(f"{bad} -> {good}" for bad, good in DEFAULT_BAD_EMAIL_DOMAIN_MAP.items())
