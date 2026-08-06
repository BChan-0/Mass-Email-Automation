"""Prior contact checks that keep an address out of a new batch.

An address is blocked when a message has actually reached the person, or when a
draft to them is still sitting in the mailbox waiting to be sent. Deleting a draft
undoes the block, because nothing was ever sent and there is no longer a pending
message to duplicate.

Three sources answer that:

Gmail sent mail
    Searched per address with ``in:sent to:...``, so it catches mail sent by hand,
    from a phone, or by a different tool. Needs the read scope.

Live drafts from earlier batches
    Drafts this app created that still exist in the mailbox. Membership is confirmed
    against Gmail's own draft list rather than trusting the local record, so a draft
    deleted or sent directly in Gmail is judged on what is actually there.

Do not contact list
    Addresses named by hand, which are blocked whatever the mailbox holds.

A blocked address is never drafted. Each block carries the date of first contact so
the report can say when it happened.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .config import SENT_SEARCH_QUERY
from .gmail_client import GmailError

# Sources are ordered by how much weight they carry in the report.
SOURCE_SENT_MAIL = "gmail_sent"
SOURCE_SCHEDULED = "gmail_scheduled"
SOURCE_PRIOR_BATCH = "prior_batch"
SOURCE_SUPPRESSION = "suppression_list"

SOURCE_LABELS = {
    SOURCE_SENT_MAIL: "already emailed from this account",
    SOURCE_SCHEDULED: "a message to this address is scheduled to send",
    SOURCE_PRIOR_BATCH: "a draft to this address is still waiting in Gmail",
    SOURCE_SUPPRESSION: "on the do not contact list",
}


def normalize_address(value: str) -> str:
    """Reduce an address to a comparison key.

    Gmail treats the local part case insensitively in practice, and a display name
    or angle brackets around the address should not defeat a match.

    :param value: raw address, possibly with a display name
    :returns: lowercased bare address, or empty when none can be found
    """
    text = (value or "").strip()
    match = re.search(r"<([^>]+)>", text)
    if match:
        text = match.group(1)
    return text.strip().strip("<>").lower()


@dataclass
class PriorContact:
    """Evidence that an address has been contacted before."""

    email: str
    source: str
    first_contact: str = ""
    last_contact: str = ""
    message_count: int = 0
    detail: str = ""

    @property
    def label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    def to_dict(self) -> dict[str, object]:
        return {
            "email": self.email,
            "source": self.source,
            "label": self.label,
            "first_contact": self.first_contact,
            "last_contact": self.last_contact,
            "message_count": self.message_count,
            "detail": self.detail,
        }


@dataclass
class HistoryIndex:
    """Addresses with a draft from this app still waiting in the mailbox.

    :param first_seen: address to earliest date a live draft was created
    :param last_seen: address to most recent date
    :param counts: address to how many live drafts exist for it
    """

    first_seen: dict[str, str] = field(default_factory=dict)
    last_seen: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def record(self, email: str, when: str) -> None:
        """Note one live draft to an address.

        Batch timestamps carry a time and zone, but the report only shows dates, so
        they are trimmed here to match the dates sent mail lookups produce.
        """
        key = normalize_address(email)
        if not key:
            return
        when = (when or "")[:10]
        existing_first = self.first_seen.get(key)
        if existing_first is None or (when and when < existing_first):
            self.first_seen[key] = when
        existing_last = self.last_seen.get(key)
        if existing_last is None or (when and when > existing_last):
            self.last_seen[key] = when
        self.counts[key] = self.counts.get(key, 0) + 1

    def lookup(self, email: str) -> PriorContact | None:
        """Return prior contact for an address, or None when it is new."""
        key = normalize_address(email)
        if key not in self.counts:
            return None
        return PriorContact(
            email=email,
            source=SOURCE_PRIOR_BATCH,
            first_contact=self.first_seen.get(key, ""),
            last_contact=self.last_seen.get(key, ""),
            message_count=self.counts[key],
            detail="an unsent draft to this address is still in the mailbox",
        )


def build_history_index(batches, exclude_batch_id: str = "", live_draft_ids: set[str] | None = None) -> HistoryIndex:
    """Index addresses whose earlier drafts are still waiting in the mailbox.

    A draft deleted through this app is skipped, and so is one that no longer exists
    in Gmail, whether it was deleted or sent there. Deleting a draft therefore clears
    the block, because nothing was sent and no pending message remains.

    When ``live_draft_ids`` is None the local record is trusted on its own, which is
    the best available answer with no read access to the mailbox.

    :param batches: batch records to read
    :param exclude_batch_id: batch to leave out, normally the run in progress
    :param live_draft_ids: draft ids that currently exist in Gmail
    :returns: an index keyed by normalized address
    """
    index = HistoryIndex()
    for batch in batches:
        if exclude_batch_id and batch.batch_id == exclude_batch_id:
            continue
        for draft in batch.drafts:
            if draft.deleted_at is not None:
                continue
            if live_draft_ids is not None and draft.draft_id not in live_draft_ids:
                continue
            index.record(draft.to, batch.created_at)
    return index


def load_suppression_list(path) -> dict[str, str]:
    """Read a do not contact list.

    One address per line. A comma or whitespace after the address starts a note,
    which is shown in the report. Blank lines and lines starting with # are
    ignored, so a list can carry comments.

    :param path: file to read, may not exist
    :returns: normalized address to note
    """
    if not path.exists():
        return {}
    entries: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}

    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        # The address runs until a comma, tab, or run of spaces; the rest is a note.
        parts = re.split(r"[,\t]|\s{2,}", text, maxsplit=1)
        key = normalize_address(parts[0])
        if key:
            entries[key] = parts[1].strip() if len(parts) > 1 else ""
    return entries


def _header(message: dict, name: str) -> str:
    """Pull one header value out of a metadata format message."""
    headers = (message.get("payload") or {}).get("headers") or []
    for header in headers:
        if (header.get("name") or "").lower() == name.lower():
            return header.get("value") or ""
    return ""


def _message_date(message: dict) -> str:
    """Best effort date for a sent message, as an ISO 8601 date."""
    raw_date = _header(message, "Date")
    if raw_date:
        try:
            return parsedate_to_datetime(raw_date).date().isoformat()
        except (TypeError, ValueError):
            pass

    # internalDate is milliseconds since the epoch and is always present.
    internal = message.get("internalDate")
    if internal:
        try:
            stamp = datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
            return stamp.date().isoformat()
        except (TypeError, ValueError, OSError):
            pass
    return ""


class SentMailChecker:
    """Look up whether an address has been emailed from the connected mailbox.

    Results are cached per address so re-running a preview does not repeat calls.
    """

    def __init__(self, service, *, enabled: bool = True) -> None:
        self._service = service
        self._enabled = enabled
        self._cache: dict[str, PriorContact | None] = {}
        self.errors: list[dict[str, str]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled and self._service is not None

    def check(self, email: str) -> PriorContact | None:
        """Return prior contact from sent mail, or None.

        A lookup failure returns None and is recorded in ``errors`` rather than
        raised, so a transient API problem cannot block a whole batch. The report
        surfaces the count so a silent all clear is never mistaken for a real one.
        """
        if not self.enabled:
            return None
        key = normalize_address(email)
        if not key:
            return None
        if key in self._cache:
            return self._cache[key]

        try:
            found = self._search(key)
        except GmailError as error:
            self.errors.append({"email": email, "error": str(error)})
            self._cache[key] = None
            return None

        self._cache[key] = found
        return found

    def _search(self, key: str) -> PriorContact | None:
        """Search sent mail for an address and summarize the oldest match."""
        messages = self._service.search_sent(SENT_SEARCH_QUERY.format(email=key))
        if not messages:
            return None

        dates = []
        for message in messages:
            # Confirm the address is really a recipient rather than a substring
            # match inside a body or another header.
            recipients = " ".join(_header(message, name) for name in ("To", "Cc", "Bcc", "Delivered-To")).lower()
            if key and key not in recipients:
                continue
            stamp = _message_date(message)
            if stamp:
                dates.append(stamp)

        if not dates:
            return None
        dates.sort()
        return PriorContact(
            email=key,
            source=SOURCE_SENT_MAIL,
            first_contact=dates[0],
            last_contact=dates[-1],
            message_count=len(dates),
            detail="found in this account's sent mail",
        )


def fetch_live_draft_ids(service) -> tuple[set[str] | None, str]:
    """Ask Gmail which drafts still exist.

    Listing drafts needs only the compose scope, so this works even when the sent
    mail search is turned off.

    :param service: authorized Gmail service, or None
    :returns: the ids and an empty string, or None and the reason it failed
    """
    if service is None:
        return None, "no Gmail connection"
    try:
        return service.list_draft_ids(), ""
    except GmailError as error:
        return None, str(error)


class ContactGuard:
    """Decide whether an address may be drafted to.

    Checks the cheap local sources first and only queries Gmail when they pass, so
    a suppressed address costs no API call.
    """

    def __init__(
        self,
        *,
        history: HistoryIndex | None = None,
        suppression: dict[str, str] | None = None,
        sent_checker: SentMailChecker | None = None,
        scheduled: dict[str, object] | None = None,
    ) -> None:
        self._history = history or HistoryIndex()
        self._suppression = suppression or {}
        self._sent = sent_checker
        # Address to the scheduled message aimed at it, from app.mailbox.
        self._scheduled = scheduled or {}

    @property
    def sent_errors(self) -> list[dict[str, str]]:
        """Sent mail lookups that failed, so the report can say coverage was partial."""
        return self._sent.errors if self._sent else []

    @property
    def checked_sent_mail(self) -> bool:
        return bool(self._sent and self._sent.enabled)

    def check(self, email: str) -> PriorContact | None:
        """Return the reason to block an address, or None to allow it."""
        key = normalize_address(email)
        if not key:
            return None

        if key in self._suppression:
            return PriorContact(
                email=email,
                source=SOURCE_SUPPRESSION,
                detail=self._suppression[key] or "listed in do-not-contact.txt",
            )

        # A scheduled message is about to go out, so it counts as contact even
        # though nothing has been sent yet.
        pending = self._scheduled.get(key)
        if pending is not None:
            return PriorContact(
                email=email,
                source=SOURCE_SCHEDULED,
                first_contact=(getattr(pending, "send_at", "") or "")[:31],
                message_count=1,
                detail="Gmail is holding a message for this address to send later",
            )

        prior_batch = self._history.lookup(email)
        if prior_batch is not None:
            return prior_batch

        if self._sent is not None:
            return self._sent.check(email)
        return None
