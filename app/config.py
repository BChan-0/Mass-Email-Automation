"""Filesystem layout and runtime limits."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Least privilege scope that still allows creating, listing, and deleting drafts.
# gmail.compose cannot read the inbox.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]

# Gmail rejects messages over 25 MB total, so keep attachments under that to leave
# room for the body and MIME overhead. The CSV limit is unrelated to Gmail and only
# bounds how much a single upload can hold in memory.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_CSV_BYTES = 25 * 1024 * 1024
MAX_CONTACTS = 2000

# Number of rendered drafts returned by the preview endpoint.
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
    def client_secret_file(self) -> Path:
        return self.credentials / "client_secret.json"

    @property
    def token_file(self) -> Path:
        return self.credentials / "token.json"

    def ensure(self) -> None:
        """Create every directory this app writes to.

        Uploaded CSVs and staged attachments are held in memory rather than on
        disk, so only settings, batch records, and credentials need a home.
        """
        for directory in (self.data, self.batches, self.credentials):
            directory.mkdir(parents=True, exist_ok=True)


def paths_from_env() -> Paths:
    """Build a Paths rooted at GDB_ROOT, or at the repo checkout by default."""
    root = os.environ.get("GDB_ROOT")
    return Paths(root=Path(root).expanduser().resolve() if root else PROJECT_ROOT)
