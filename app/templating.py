"""Placeholder substitution for subject lines, bodies, and sign offs.

Placeholders look like ``{{company}}``. A default after a pipe is used when the
contact has no value for that field: ``{{first_name|there}}``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*(?:\|([^{}]*))?\}\}")

# Fields the app always provides, shown as clickable chips in the UI.
KNOWN_FIELDS = (
    "first_name",
    "last_name",
    "full_name",
    "email",
    "company",
    "title",
    "sender_name",
    "signoff",
)


def normalize_key(name: str) -> str:
    """Turn a CSV header or placeholder name into a lowercase identifier."""
    cleaned = re.sub(r"[^0-9a-zA-Z]+", "_", (name or "").strip())
    return cleaned.strip("_").lower()


def find_placeholders(text: str) -> list[str]:
    """List placeholder names in order of first appearance, without duplicates."""
    seen: list[str] = []
    for match in PLACEHOLDER_PATTERN.finditer(text or ""):
        key = normalize_key(match.group(1))
        if key not in seen:
            seen.append(key)
    return seen


@dataclass
class RenderResult:
    """Rendered text plus the placeholders that had no value."""

    text: str
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


def render(text: str, context: dict[str, str]) -> RenderResult:
    """Substitute placeholders in ``text`` using ``context``.

    A placeholder with no value and no default is left in place and reported in
    ``missing`` so the caller can skip the row instead of mailing raw syntax.

    :param text: template text containing placeholders
    :param context: field name to value, keys already normalized
    :returns: the rendered text and any unresolved placeholder names
    """
    missing: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        key = normalize_key(match.group(1))
        fallback = match.group(2)
        value = (context.get(key) or "").strip()
        if value:
            return value
        if fallback is not None:
            return fallback.strip()
        if key not in missing:
            missing.append(key)
        return match.group(0)

    return RenderResult(text=PLACEHOLDER_PATTERN.sub(substitute, text or ""), missing=missing)


def build_context(contact: dict[str, str], sender_name: str, signoff: str) -> dict[str, str]:
    """Merge a contact row with the sender fields shared by the whole batch.

    Every CSV column is available under its normalized header, so a template can
    reference columns this app does not know about.
    """
    context = {normalize_key(key): (value or "") for key, value in contact.items()}
    context["sender_name"] = sender_name or ""
    context["signoff"] = signoff or ""
    return context
