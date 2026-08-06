"""Filesystem layout and runtime limits."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# gmail.compose creates, lists, and deletes drafts. gmail.readonly is needed to
# search sent mail for prior contact; gmail.metadata would be narrower but cannot
# use a search query, which would mean walking the whole mailbox instead.
COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SCOPES = [COMPOSE_SCOPE, READ_SCOPE]

# No date window, so prior contact of any age counts. Kept as a constant so the
# meaning of "ever emailed" stays in one place.
SENT_SEARCH_QUERY = "in:sent to:{email}"

# Messages Gmail is holding to send later. They are not in the drafts list and carry
# no system label, so this search is the only way to find them.
SCHEDULED_SEARCH_QUERY = "in:scheduled"

# Sent mail lookups cost one API call per contact, so a large list takes a while.
# The prior contact report warns past this many contacts rather than looking stalled.
HISTORY_SLOW_THRESHOLD = 250

# Gmail rejects messages over 25 MB total, so keep attachments under that to leave
# room for the body and MIME overhead. The CSV limit is unrelated to Gmail and only
# bounds how much a single upload can hold in memory.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_CSV_BYTES = 25 * 1024 * 1024
MAX_CONTACTS = 2000

# Default number of rendered drafts from the preview endpoint. A request may ask for
# more, up to a clamp of 25.
PREVIEW_LIMIT = 5


@dataclass(frozen=True)
class Paths:
    """Directories and files the app reads and writes."""

    root: Path

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def batches(self) -> Path:
        return self.data / "batches"

    @property
    def credentials(self) -> Path:
        return self.root / "credentials"

    @property
    def settings_file(self) -> Path:
        return self.data / "settings.json"

    @property
    def suppression_file(self) -> Path:
        """Addresses never to contact, one per line.

        Appended to by the block command and the UI, and safe to edit by hand.
        """
        return self.data / "do-not-contact.txt"

    @property
    def library(self) -> Path:
        """Remembered CSV uploads and the edits made to them."""
        return self.data / "library"

    @property
    def tracker_file(self) -> Path:
        """Per contact spreadsheet values typed in the UI."""
        return self.data / "tracker.json"

    @property
    def client_secret_file(self) -> Path:
        return self.credentials / "client_secret.json"

    @property
    def token_file(self) -> Path:
        return self.credentials / "token.json"

    def ensure(self) -> None:
        """Create every directory this app writes to.

        Staged attachments are held in memory, but uploads are saved to the library,
        so settings, batch records, saved lists, and credentials all need a home.
        """
        for directory in (self.data, self.batches, self.library, self.credentials):
            directory.mkdir(parents=True, exist_ok=True)


def paths_from_env() -> Paths:
    """Build a Paths rooted at GDB_ROOT, or at the repo checkout by default."""
    root = os.environ.get("GDB_ROOT")
    return Paths(root=Path(root).expanduser().resolve() if root else PROJECT_ROOT)
