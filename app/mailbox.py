"""Reading the current state of outreach messages from Gmail.

Four states matter: draft (in the drafts list), scheduled (held for a later send),
sent, and deleted (this app created it and it is gone from Gmail).

Scheduled messages are the awkward one. They are absent from the drafts list and
carry no system label, so ``in:scheduled`` is the only way to find them, and their
Date header holds the send time rather than a creation time.

The status view and the prior contact check both read from here, so they cannot
disagree about what is pending.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

from .gmail_client import GmailError
from .history import normalize_address

STATUS_DRAFT = "draft"
STATUS_SCHEDULED = "scheduled"
STATUS_SENT = "sent"
STATUS_DELETED = "deleted"

STATUS_LABELS = {
    STATUS_DRAFT: "Draft",
    STATUS_SCHEDULED: "Scheduled to send",
    STATUS_SENT: "Sent",
    STATUS_DELETED: "Deleted",
}

# Order used when one address has more than one message, most committed first. A
# sent message outranks a scheduled one, which outranks an unsent draft.
STATUS_PRECEDENCE = (STATUS_SENT, STATUS_SCHEDULED, STATUS_DRAFT, STATUS_DELETED)


def _header(message: dict, name: str) -> str:
    """Pull one header value out of a metadata format message."""
    for header in (message.get("payload") or {}).get("headers") or []:
        if (header.get("name") or "").lower() == name.lower():
            return header.get("value") or ""
    return ""


def to_iso_date(value: str) -> str:
    """Reduce a date to YYYY-MM-DD.

    Mail headers carry a full RFC 2822 date, which a spreadsheet reads as text. A
    plain date sorts and filters correctly instead.

    :param value: header date, ISO timestamp, or empty
    :returns: the date part, or an empty string when it cannot be parsed
    """
    text = (value or "").strip()
    if not text:
        return ""
    try:
        return parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError):
        # Already an ISO timestamp from a batch record.
        return text[:10] if len(text) >= 10 and text[4] == "-" else ""


def _recipients(message: dict) -> list[str]:
    """Every address in the recipient headers of a message."""
    found: list[str] = []
    for name in ("To", "Cc", "Bcc", "Delivered-To"):
        for part in _header(message, name).split(","):
            address = normalize_address(part)
            if address and address not in found:
                found.append(address)
    return found


@dataclass
class ScheduledMessage:
    """One message Gmail is holding to send later."""

    message_id: str
    to: str
    subject: str
    send_at: str
    recipients: list[str] = field(default_factory=list)

    @property
    def send_date(self) -> str:
        """The send time as a plain date, for a spreadsheet cell."""
        return to_iso_date(self.send_at)


def read_scheduled(service, *, limit: int = 500) -> tuple[list[ScheduledMessage], str]:
    """Fetch messages Gmail will send later.

    :param service: authorized Gmail service, or None
    :param limit: how many scheduled messages to inspect
    :returns: the messages and an empty string, or an empty list and the reason
    """
    if service is None:
        return [], "no Gmail connection"
    try:
        raw = service.list_scheduled(limit=limit)
    except GmailError as error:
        return [], str(error)

    messages = []
    for item in raw:
        recipients = _recipients(item)
        messages.append(
            ScheduledMessage(
                message_id=item.get("id", ""),
                to=recipients[0] if recipients else "",
                subject=_header(item, "Subject"),
                # The Date header on a scheduled message is the time Gmail will use.
                send_at=_header(item, "Date"),
                recipients=recipients,
            )
        )
    return messages, ""


def scheduled_by_address(messages: list[ScheduledMessage]) -> dict[str, ScheduledMessage]:
    """Index scheduled messages by every address they are addressed to."""
    index: dict[str, ScheduledMessage] = {}
    for message in messages:
        for address in message.recipients:
            index.setdefault(address, message)
    return index
