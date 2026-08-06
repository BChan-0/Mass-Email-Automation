"""Rows for the outreach tracking spreadsheet.

Produces the columns the Google Sheet uses, in order, so a run can be pasted
straight in. Everything except Notes is filled from the CSV and the mailbox; Notes
is left blank for you to write. Any value the app does not have becomes an empty
cell rather than being omitted, so pasted columns stay aligned.

Values you type per contact, such as the assignee, are saved and reused the next
time the same address appears.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from pathlib import Path

from .history import normalize_address
from .mailbox import STATUS_LABELS, STATUS_PRECEDENCE, to_iso_date
from .store import _write_json

# Column headers, in the order the sheet expects them.
COLUMNS = (
    "Client",
    "Status",
    "Contact Name",
    "Contact Title",
    "Contact Email",
    "Contact LinkedIn",
    "Re-Emailed?",
    "HPL Assignee",
    "Notes",
    "Date of most recent contact",
)

# Subject lines follow "[Harvard Product Lab x CLIENT] ...", so the client name can
# be read back out of a message that was already created.
CLIENT_FROM_SUBJECT = re.compile(r"\[[^\]]*?\bx\s+([^\]]+)\]")

# CSV headers that may hold a LinkedIn URL.
LINKEDIN_ALIASES = ("person_linkedin_url", "linkedin_url", "linkedin", "person_linkedin")

# Fields the user fills in and the app remembers. Notes is deliberately included so
# a note survives to the next export, even though it is never auto filled.
REMEMBERED_FIELDS = ("client", "assignee", "notes", "re_emailed", "linkedin", "status_override")


def client_from_subject(subject: str) -> str:
    """Pull the client name out of a subject line, or return an empty string."""
    found = CLIENT_FROM_SUBJECT.search(subject or "")
    return found.group(1).strip() if found else ""


def company_domain(email: str) -> str:
    """Return the domain of an address, used as a fallback client name."""
    _, _, domain = normalize_address(email).partition("@")
    return domain


@dataclass
class TrackerRow:
    """One spreadsheet row for one contact."""

    email: str
    client: str = ""
    status: str = ""
    name: str = ""
    title: str = ""
    linkedin: str = ""
    re_emailed: str = ""
    assignee: str = ""
    notes: str = ""
    last_contact: str = ""

    def as_cells(self) -> list[str]:
        """Values in column order, with no cell left out."""
        return [
            self.client,
            self.status,
            self.name,
            self.title,
            self.email,
            self.linkedin,
            self.re_emailed,
            self.assignee,
            self.notes,
            self.last_contact,
        ]

    def to_dict(self) -> dict[str, str]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


def to_tsv(rows: list[TrackerRow], *, include_header: bool = True) -> str:
    """Render rows as tab separated text.

    Tabs are used because pasting a tab separated block into Google Sheets fills one
    cell per value without an import step. Any tab or newline inside a value is
    turned into a space so it cannot break the column alignment.

    :param rows: rows to render
    :param include_header: write the column names as the first line
    :returns: text ready to copy into a spreadsheet
    """

    def clean(value: str) -> str:
        return " ".join(str(value or "").split())

    lines = ["\t".join(COLUMNS)] if include_header else []
    lines.extend("\t".join(clean(cell) for cell in row.as_cells()) for row in rows)
    return "\n".join(lines)


class TrackerFields:
    """Per contact values the user typed, saved between sessions."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: dict[str, dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            saved = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(saved, dict):
            self._entries = {
                normalize_address(key): value
                for key, value in saved.items()
                if isinstance(value, dict) and normalize_address(key)
            }

    def get(self, email: str) -> dict[str, str]:
        """Saved values for one address, empty when nothing was typed."""
        return dict(self._entries.get(normalize_address(email), {}))

    def update(self, email: str, values: dict[str, object]) -> dict[str, str]:
        """Merge typed values for one address and keep them.

        Only the remembered fields are stored, so an unexpected key cannot grow the
        file. An empty string is kept, since clearing a value is a real edit.
        """
        key = normalize_address(email)
        if not key:
            return {}
        entry = self._entries.setdefault(key, {})
        for name in REMEMBERED_FIELDS:
            if name in values:
                entry[name] = str(values[name] or "")
        return dict(entry)

    def save(self) -> None:
        """Write every saved value to disk."""
        _write_json(self._path, self._entries)

    def count(self) -> int:
        return len(self._entries)


@dataclass
class MessageState:
    """What the mailbox currently says about one address."""

    status: str = ""
    subject: str = ""
    when: str = ""
    seen_statuses: set[str] = field(default_factory=set)
    # Ids of distinct messages aimed at this address, so the same message showing up
    # under two statuses is not mistaken for two rounds of contact.
    message_keys: set[str] = field(default_factory=set)

    @property
    def re_emailed(self) -> str:
        """Yes once a second distinct message has gone to this address."""
        return "Yes" if len(self.message_keys) > 1 else "No"


def build_rows(
    contacts,
    *,
    states: dict[str, MessageState],
    saved: TrackerFields,
    default_assignee: str = "",
) -> list[TrackerRow]:
    """Assemble one row per contact.

    Saved values win over anything derived, because they were typed deliberately.

    :param contacts: Contact objects from the parsed CSV
    :param states: address to mailbox state, from app.mailbox
    :param saved: per contact values the user typed before
    :param default_assignee: assignee applied when a contact has none saved
    :returns: rows in contact order
    """
    rows = []
    for contact in contacts:
        key = normalize_address(contact.email)
        state = states.get(key, MessageState())
        stored = saved.get(contact.email)

        linkedin = stored.get("linkedin") or _linkedin_from(contact)
        client = stored.get("client") or client_from_subject(state.subject) or contact.company
        status = stored.get("status_override") or STATUS_LABELS.get(state.status, "")

        rows.append(
            TrackerRow(
                email=contact.email,
                client=client,
                status=status,
                name=contact.full_name,
                title=contact.title,
                linkedin=linkedin,
                re_emailed=stored.get("re_emailed") or (state.re_emailed if state.status else ""),
                assignee=stored.get("assignee") or default_assignee,
                notes=stored.get("notes", ""),
                last_contact=state.when,
            )
        )
    return rows


def _linkedin_from(contact) -> str:
    """Find a LinkedIn URL among the contact's CSV columns."""
    for alias in LINKEDIN_ALIASES:
        value = (contact.extra or {}).get(alias, "")
        if value:
            return value
    for key, value in (contact.extra or {}).items():
        if "linkedin" in key and value:
            return value
    return ""


def merge_states(
    *, scheduled: dict, live_draft_ids: set[str] | None, batches, sent_lookup=None
) -> dict[str, MessageState]:
    """Work out the current state of every address this app has touched.

    Scheduled beats draft, and sent beats both, so an address with several messages
    is reported at its most committed state.

    :param scheduled: address to ScheduledMessage, from app.mailbox
    :param live_draft_ids: draft ids currently in Gmail, or None when unknown
    :param batches: batch records this app has written
    :param sent_lookup: callable taking an address and returning a PriorContact
    :returns: address to state
    """
    states: dict[str, MessageState] = {}

    def note(address: str, status: str, subject: str, when: str, message_key: str = "") -> None:
        key = normalize_address(address)
        if not key:
            return
        state = states.setdefault(key, MessageState())
        state.seen_statuses.add(status)
        if message_key:
            state.message_keys.add(message_key)
        # Keep the most committed status, and its subject and date with it.
        if state.status == "" or STATUS_PRECEDENCE.index(status) < STATUS_PRECEDENCE.index(state.status):
            state.status = status
            state.subject = subject or state.subject
            state.when = when or state.when

    for batch in batches:
        for draft in batch.drafts:
            # The message id identifies the message itself, so the same one seen as a
            # draft and then as scheduled counts once.
            key = draft.message_id or draft.draft_id
            if draft.deleted_at is not None:
                # A deleted draft was never sent, so it is recorded for the status
                # view but does not count as a round of contact.
                note(draft.to, "deleted", draft.subject, draft.deleted_at[:10])
            elif live_draft_ids is None or draft.draft_id in live_draft_ids:
                note(draft.to, "draft", draft.subject, batch.created_at[:10], key)
            else:
                # Gone from Gmail without this app deleting it, so it was sent or
                # removed there. Sent mail below decides which, and only a sent
                # message counts toward re-emailed.
                note(draft.to, "deleted", draft.subject, batch.created_at[:10])

    for address, message in scheduled.items():
        note(address, "scheduled", message.subject, to_iso_date(message.send_at), message.message_id)

    if sent_lookup is not None:
        # A draft that has left the mailbox reads as deleted until sent mail confirms
        # it was sent, so those addresses are the ones that most need checking.
        for key in sorted(states, key=lambda item: states[item].status != "deleted"):
            found = sent_lookup(key)
            if found is None:
                continue
            state = states[key]
            note(key, "sent", state.subject, found.last_contact or found.first_contact)
            # Sent mail reports how many messages it found, which is the reliable
            # signal that this person was contacted more than once.
            if found.message_count > 1:
                state.message_keys.update(f"sent-{key}-{n}" for n in range(found.message_count))

    return states
