"""CSV loading and contact normalization.

Apollo exports carry dozens of columns with inconsistent names across accounts,
so headers are matched against the alias tables below rather than by position.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from .templating import normalize_key

# Header aliases, most specific first. Matching is done on normalized headers.
EMAIL_ALIASES = ("email", "email_address", "work_email", "primary_email", "contact_email")
FIRST_NAME_ALIASES = ("first_name", "firstname", "given_name", "fname")
LAST_NAME_ALIASES = ("last_name", "lastname", "family_name", "surname", "lname")
FULL_NAME_ALIASES = ("name", "full_name", "fullname", "contact_name", "person_name")
COMPANY_ALIASES = ("company", "company_name", "organization", "organization_name", "account_name", "employer")
TITLE_ALIASES = ("title", "job_title", "position", "role", "headline")

# Aliases too generic to match as a substring. Apollo exports carry headers such as
# "Company Name for Emails", where a substring pass for "name" would pick the wrong
# column. These still match when a header equals them exactly.
EXACT_ONLY_ALIASES = frozenset({"name", "role"})

# Deliberately permissive: Gmail is the real validator. This only catches rows
# that are obviously not addresses, such as blanks and Apollo's lock markers.
EMAIL_PATTERN = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$")

# Apollo writes these into the email column when an address is gated or bounced.
PLACEHOLDER_EMAILS = frozenset({"email_not_unlocked", "not_unlocked", "n/a", "na", "none", "-", "unknown"})


@dataclass
class Contact:
    """One recipient plus every column from that CSV row."""

    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    title: str = ""
    row_number: int = 0
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part).strip()

    def as_context(self) -> dict[str, str]:
        """Flatten to a template context, with named fields overriding raw columns."""
        context = dict(self.extra)
        context.update(
            {
                "email": self.email,
                "first_name": self.first_name,
                "last_name": self.last_name,
                "full_name": self.full_name,
                "company": self.company,
                "title": self.title,
            }
        )
        return context


@dataclass
class SkippedRow:
    """A row that was dropped, with the reason shown to the user."""

    row_number: int
    email: str
    reason: str


@dataclass
class ParseResult:
    """Contacts kept, rows dropped, and the headers that were recognized."""

    contacts: list[Contact] = field(default_factory=list)
    skipped: list[SkippedRow] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    detected: dict[str, str] = field(default_factory=dict)


def _pick(headers: dict[str, str], aliases: tuple[str, ...], claimed: set[str]) -> str | None:
    """Return the original header matching the first alias present, else None.

    An exact alias match wins over a substring match, and any header already
    claimed by an earlier field is passed over. Without that, the loose substring
    pass would match "name" inside "first_name" and treat the first name column as
    a full name column as well.

    :param headers: normalized header to original header
    :param aliases: candidate names, most specific first
    :param claimed: original headers already assigned to another field
    :returns: the original header to use, or None
    """
    for alias in aliases:
        original = headers.get(alias)
        if original is not None and original not in claimed:
            return original
    for alias in aliases:
        if alias in EXACT_ONLY_ALIASES:
            continue
        for normalized, original in headers.items():
            if alias in normalized and original not in claimed:
                return original
    return None


def _split_full_name(value: str) -> tuple[str, str]:
    """Split a display name into first and last, keeping compound surnames whole."""
    parts = [part for part in (value or "").split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def is_valid_email(value: str) -> bool:
    """Check an address well enough to skip empty and placeholder cells."""
    candidate = (value or "").strip().strip("<>").lower()
    if not candidate or candidate in PLACEHOLDER_EMAILS:
        return False
    return bool(EMAIL_PATTERN.match(candidate))


def parse_csv(text: str, max_contacts: int) -> ParseResult:
    """Parse CSV text into contacts, dropping rows without a usable address.

    Duplicate addresses are kept once, at their first appearance, so a re-exported
    Apollo list does not produce two drafts for the same person.

    :param text: full CSV contents including the header row
    :param max_contacts: upper bound on kept rows, to bound a single batch
    :returns: contacts, skipped rows, and the header mapping that was detected
    """
    result = ParseResult()
    # utf-8-sig handles the BOM Excel adds when re-saving an Apollo export.
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        return result

    result.headers = [name for name in reader.fieldnames if name]
    normalized = {}
    for name in result.headers:
        key = normalize_key(name)
        if key and key not in normalized:
            normalized[key] = name

    # Claimed headers accumulate so two fields never resolve to the same column.
    # Order matters: the more specific fields are matched before the looser ones.
    claimed: set[str] = set()

    def claim(aliases: tuple[str, ...]) -> str | None:
        chosen = _pick(normalized, aliases, claimed)
        if chosen is not None:
            claimed.add(chosen)
        return chosen

    email_col = claim(EMAIL_ALIASES)
    first_col = claim(FIRST_NAME_ALIASES)
    last_col = claim(LAST_NAME_ALIASES)
    full_col = claim(FULL_NAME_ALIASES)
    company_col = claim(COMPANY_ALIASES)
    title_col = claim(TITLE_ALIASES)

    result.detected = {
        key: value
        for key, value in {
            "email": email_col,
            "first_name": first_col,
            "last_name": last_col,
            "full_name": full_col,
            "company": company_col,
            "title": title_col,
        }.items()
        if value
    }

    if not email_col:
        return result

    seen: set[str] = set()
    for index, row in enumerate(reader, start=2):
        if len(result.contacts) >= max_contacts:
            result.skipped.append(SkippedRow(index, "", f"row limit of {max_contacts} reached"))
            break

        email = (row.get(email_col) or "").strip().strip("<>")
        if not is_valid_email(email):
            result.skipped.append(SkippedRow(index, email, "missing or unusable email address"))
            continue

        key = email.lower()
        if key in seen:
            result.skipped.append(SkippedRow(index, email, "duplicate address"))
            continue
        seen.add(key)

        first = (row.get(first_col) or "").strip() if first_col else ""
        last = (row.get(last_col) or "").strip() if last_col else ""
        if not first and full_col:
            first, derived_last = _split_full_name((row.get(full_col) or "").strip())
            last = last or derived_last

        extra = {
            normalize_key(name): (value or "").strip() for name, value in row.items() if name and normalize_key(name)
        }

        result.contacts.append(
            Contact(
                email=email,
                first_name=first,
                last_name=last,
                company=(row.get(company_col) or "").strip() if company_col else "",
                title=(row.get(title_col) or "").strip() if title_col else "",
                row_number=index,
                extra=extra,
            )
        )

    return result
