"""Gmail API access: OAuth handling and draft create, list, and delete."""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import GMAIL_SCOPES, REPLY_SEARCH_QUERY, SCHEDULED_SEARCH_QUERY
from .message import encode_message

# Retried on the assumption the request itself is fine: rate limits and transient
# server errors. Note 403 also covers scope and permission errors, which retrying
# cannot fix.
RETRYABLE_STATUS = frozenset({403, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 5

# Headers fetched for a message. Bodies are never requested. Subject is included so
# the status view can name a message without a second call.
METADATA_HEADERS = ["To", "Cc", "Bcc", "Delivered-To", "Date", "Subject", "From"]


# Senders that mean delivery failed rather than a person answering. A bounce lands in
# the thread it failed on, so it looks like a reply unless it is named.
BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@")


def _bare_address(value: str) -> str:
    """Reduce a From header to a lowercase address, dropping any display name."""
    text = (value or "").strip()
    if "<" in text and ">" in text:
        text = text[text.index("<") + 1 : text.index(">")]
    return text.strip().lower()


def is_bounce_sender(address: str) -> bool:
    """True when an address is a mail system reporting a failure."""
    return any(address.startswith(marker) for marker in BOUNCE_SENDERS)


def _header_value(message: dict, name: str) -> str:
    """Read one header out of a metadata format message."""
    for header in (message.get("payload") or {}).get("headers") or []:
        if (header.get("name") or "").lower() == name.lower():
            return header.get("value") or ""
    return ""


class AuthError(RuntimeError):
    """Raised when no usable credentials are available."""


class GmailError(RuntimeError):
    """Raised when the Gmail API rejects a request."""


@dataclass
class DraftRef:
    """Identifiers for a draft that was created."""

    draft_id: str
    message_id: str
    to: str
    subject: str


def load_credentials(token_file: Path) -> Credentials | None:
    """Load saved credentials and refresh them if they have expired.

    A token that can no longer be refreshed is removed, so the UI reports a clean
    disconnected state rather than retrying a grant that is gone.

    :param token_file: path to the stored OAuth token
    :returns: usable credentials, or None if the user needs to authorize
    """
    if not token_file.exists():
        return None
    try:
        credentials = Credentials.from_authorized_user_file(str(token_file), GMAIL_SCOPES)
    except (ValueError, json.JSONDecodeError):
        return None

    if credentials.valid:
        return credentials
    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception:
            # Refresh fails once the grant is gone. While the OAuth app's publishing
            # status is Testing with an external user type, Google expires the refresh
            # token seven days after consent, so this is routine rather than a fault.
            # Publishing the app removes that expiry. See README, Google Cloud setup.
            token_file.unlink(missing_ok=True)
            return None
        token_file.write_text(credentials.to_json(), encoding="utf-8")
        token_file.chmod(0o600)
        return credentials
    return None


def run_local_authorization(client_secret_file: Path, token_file: Path, port: int = 0) -> Credentials:
    """Run the installed app OAuth flow and save the resulting token.

    Opens a browser and listens on a loopback port for the redirect, so this only
    works when run on the same machine as the browser.

    :param client_secret_file: OAuth client secret downloaded from Google Cloud
    :param token_file: path the token is written to
    :param port: loopback port, or 0 to let the OS choose
    :returns: authorized credentials
    """
    if not client_secret_file.exists():
        raise AuthError(
            f"OAuth client secret not found at {client_secret_file}. See README section 'Google Cloud setup'."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_file), GMAIL_SCOPES)
    credentials = flow.run_local_server(port=port, prompt="consent")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")
    token_file.chmod(0o600)
    return credentials


def _reason(error: HttpError) -> str:
    """Pull a readable message out of a Gmail API error."""
    try:
        payload = json.loads(error.content.decode("utf-8"))
        return payload.get("error", {}).get("message") or str(error)
    except Exception:
        return str(error)


def _with_retry(request_factory, description: str):
    """Execute a Gmail request, retrying rate limits and transient server errors.

    ``request_factory`` is a callable so each attempt gets a fresh request object;
    httplib2 request objects are not safe to execute twice.
    """
    delay = 1.0
    last_error: HttpError | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return request_factory().execute()
        except HttpError as error:
            status = getattr(error.resp, "status", None)
            if status not in RETRYABLE_STATUS or attempt == MAX_ATTEMPTS:
                raise GmailError(f"{description}: {_reason(error)}") from error
            last_error = error
            time.sleep(delay + random.uniform(0, 0.4))
            delay = min(delay * 2, 16.0)
    raise GmailError(f"{description}: {_reason(last_error)}" if last_error else description)


class GmailDraftService:
    """Thin wrapper over the Gmail drafts endpoints."""

    def __init__(self, credentials: Credentials) -> None:
        # cache_discovery is off because the file cache warns when no writable
        # cache directory is available.
        self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        self._own: str | None = None

    def profile_email(self) -> str:
        """Return the address of the authorized mailbox."""
        profile = _with_retry(lambda: self._service.users().getProfile(userId="me"), "could not read Gmail profile")
        return profile.get("emailAddress", "")

    def _own_address(self) -> str:
        """The authorized mailbox address, fetched once and kept."""
        if self._own is None:
            self._own = self.profile_email().lower()
        return self._own

    def create_draft(self, message, *, to: str, subject: str) -> DraftRef:
        """Create one draft and return its identifiers."""
        payload = {"message": {"raw": encode_message(message)}}
        created = _with_retry(
            lambda: self._service.users().drafts().create(userId="me", body=payload),
            f"could not create draft for {to}",
        )
        return DraftRef(
            draft_id=created.get("id", ""),
            message_id=(created.get("message") or {}).get("id", ""),
            to=to,
            subject=subject,
        )

    def list_draft_ids(self) -> set[str]:
        """Return the ids of every draft currently in the mailbox.

        One paginated listing rather than a lookup per recorded draft, so checking
        whether earlier drafts are still live costs a couple of calls instead of one
        per contact.

        :returns: draft ids that exist right now
        """
        identifiers: set[str] = set()
        page_token = None
        while True:
            response = _with_retry(
                lambda token=page_token: (
                    self._service.users().drafts().list(userId="me", maxResults=500, pageToken=token)
                ),
                "could not list drafts",
            )
            for draft in response.get("drafts") or []:
                if draft.get("id"):
                    identifiers.add(draft["id"])
            page_token = response.get("nextPageToken")
            if not page_token:
                return identifiers

    def list_scheduled(self, *, limit: int = 500) -> list[dict]:
        """Return header metadata for messages Gmail is holding to send later.

        Scheduled messages are not in the drafts list and carry no system label, so
        the ``in:scheduled`` search is the only way to find them. Their Date header
        holds the send time Gmail will use.

        :param limit: how many scheduled messages to inspect
        :returns: messages in metadata format, empty when none are scheduled
        """
        return self.search_sent(SCHEDULED_SEARCH_QUERY, limit=limit)

    def list_reply_senders(self, *, limit: int = 500) -> tuple[set[str], set[str]]:
        """Return who answered, and whose address bounced.

        A reply lands in the same thread as the message it answers, so inbound mail on
        a sent thread is an answer. A delivery failure lands in that thread too, which
        is why bounce senders are separated out rather than counted as replies. Only
        the From header is read, never a body.

        :param limit: how many threads to inspect
        :returns: addresses that replied, and addresses of threads that bounced
        """
        listing = _with_retry(
            lambda: self._service.users().threads().list(userId="me", q=REPLY_SEARCH_QUERY, maxResults=limit),
            "could not search for replies",
        )
        senders: set[str] = set()
        bounced: set[str] = set()
        for thread in listing.get("threads") or []:
            thread_id = thread.get("id")
            if not thread_id:
                continue
            detail = _with_retry(
                lambda thread_id=thread_id: (
                    self._service.users()
                    .threads()
                    .get(userId="me", id=thread_id, format="metadata", metadataHeaders=["From", "To"])
                ),
                f"could not read thread {thread_id}",
            )
            messages = detail.get("messages") or []
            inbound = []
            for message in messages:
                # A message this mailbox sent carries SENT, so anything without it in a
                # sent thread came from elsewhere.
                if "SENT" in (message.get("labelIds") or []):
                    continue
                inbound.append(_bare_address(_header_value(message, "From")))

            if any(is_bounce_sender(address) for address in inbound):
                # The bounce report names the failed address in its body, which is not
                # read here, so the recipient of the outgoing message is used instead.
                # A thread addressed to the mailbox itself, or to several people, gives
                # no single answer, so those are left out rather than guessed at.
                recipients = {
                    _bare_address(_header_value(message, "To"))
                    for message in messages
                    if "SENT" in (message.get("labelIds") or [])
                }
                recipients.discard("")
                recipients.discard(self._own_address())
                if len(recipients) == 1:
                    bounced.update(recipients)
            senders.update(address for address in inbound if not is_bounce_sender(address))

        senders.discard("")
        bounced.discard("")
        return senders, bounced

    def search_sent(self, query: str, *, limit: int = 20) -> list[dict]:
        """Search the mailbox and return message headers for the matches.

        Only headers are fetched, never bodies, which keeps the response small and
        means message text is not pulled into this process.

        :param query: Gmail search query, the same syntax as the search box
        :param limit: how many matches to inspect
        :returns: messages in metadata format, empty when nothing matches
        """
        listing = _with_retry(
            lambda: (
                self._service.users().messages().list(userId="me", q=query, maxResults=limit, includeSpamTrash=False)
            ),
            "could not search sent mail",
        )
        identifiers = [item.get("id") for item in listing.get("messages") or [] if item.get("id")]

        return [self._message_metadata(message_id) for message_id in identifiers]

    def _message_metadata(self, message_id: str) -> dict:
        """Fetch one message's headers, without its body."""
        return _with_retry(
            lambda: (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata", metadataHeaders=METADATA_HEADERS)
            ),
            f"could not read message {message_id}",
        )

    def delete_draft(self, draft_id: str) -> None:
        """Delete one draft. Already deleted drafts are treated as success."""
        try:
            _with_retry(
                lambda: self._service.users().drafts().delete(userId="me", id=draft_id),
                f"could not delete draft {draft_id}",
            )
        except GmailError as error:
            if "404" in str(error) or "not found" in str(error).lower():
                return
            raise
