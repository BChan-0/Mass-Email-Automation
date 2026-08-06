"""Saved CSV uploads, so a list can be reused without finding the file again.

Each saved list keeps the original upload byte for byte plus any edits made to it.
Reloading replays those edits and marks the changed cells, so it is obvious what
differs from the export. The original is never overwritten, so a list can always be
read back as Apollo produced it.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .store import _write_json

# Fields an edit may change, matching what the contact table exposes.
EDITABLE_FIELDS = ("email", "first_name", "last_name", "company", "title")

MAX_SAVED_LISTS = 50


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_slug(name: str) -> str:
    """Reduce a filename to something safe to use inside a path."""
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", (name or "").strip()).strip("-._")
    return (cleaned or "contacts")[:60]


@dataclass
class SavedList:
    """One remembered upload."""

    list_id: str
    name: str
    saved_at: str
    last_used_at: str = ""
    row_count: int = 0
    digest: str = ""
    # Row index to field to value, for cells the caller reported as changed.
    edits: dict[str, dict[str, str]] = field(default_factory=dict)
    removed: list[int] = field(default_factory=list)

    @property
    def edited_count(self) -> int:
        return len(self.edits)

    def to_dict(self) -> dict[str, object]:
        return {
            "list_id": self.list_id,
            "name": self.name,
            "saved_at": self.saved_at,
            "last_used_at": self.last_used_at,
            "row_count": self.row_count,
            "digest": self.digest,
            "edits": self.edits,
            "removed": self.removed,
            "edited_count": self.edited_count,
            "removed_count": len(self.removed),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> SavedList:
        raw_edits = payload.get("edits") or {}
        edits = {
            str(index): {key: str(value) for key, value in changes.items() if key in EDITABLE_FIELDS}
            for index, changes in raw_edits.items()
            if isinstance(changes, dict)
        }
        return cls(
            list_id=payload.get("list_id", ""),
            name=payload.get("name", ""),
            saved_at=payload.get("saved_at", ""),
            last_used_at=payload.get("last_used_at", ""),
            row_count=int(payload.get("row_count", 0) or 0),
            digest=payload.get("digest", ""),
            edits=edits,
            removed=[int(item) for item in payload.get("removed", []) if str(item).isdigit()],
        )


class CsvLibrary:
    """Saved uploads on disk: the raw CSV plus a record of edits."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _record_path(self, list_id: str) -> Path:
        # List ids arrive from URL segments, so this guard is what keeps a crafted id
        # inside the library directory. _csv_path repeats it for the same reason.
        if not list_id or "/" in list_id or "\\" in list_id or list_id.startswith("."):
            raise ValueError(f"invalid list id: {list_id!r}")
        return self._directory / f"{list_id}.json"

    def _csv_path(self, list_id: str) -> Path:
        if not list_id or "/" in list_id or "\\" in list_id or list_id.startswith("."):
            raise ValueError(f"invalid list id: {list_id!r}")
        return self._directory / f"{list_id}.csv"

    def save_upload(self, *, name: str, raw: bytes, row_count: int) -> SavedList:
        """Remember an upload, reusing the entry when the same file comes back.

        Sameness is the file's digest, so re-uploading a list you already saved keeps
        the edits you made to it rather than starting a second copy.

        :param name: original filename
        :param raw: the uploaded bytes, stored unchanged
        :param row_count: usable contacts parsed from it
        :returns: the saved entry
        """
        digest = hashlib.sha256(raw).hexdigest()
        for existing in self.list_all():
            if existing.digest == digest:
                existing.last_used_at = _utc_now()
                existing.row_count = row_count
                self._save_record(existing)
                return existing

        self._directory.mkdir(parents=True, exist_ok=True)
        entry = SavedList(
            list_id=f"{safe_slug(name)}-{uuid.uuid4().hex[:8]}",
            name=name,
            saved_at=_utc_now(),
            last_used_at=_utc_now(),
            row_count=row_count,
            digest=digest,
        )
        self._csv_path(entry.list_id).write_bytes(raw)
        self._save_record(entry)
        self._prune()
        return entry

    def _save_record(self, entry: SavedList) -> None:
        _write_json(self._record_path(entry.list_id), entry.to_dict())

    def _prune(self) -> None:
        """Drop the least recently used entries past the cap."""
        entries = self.list_all()
        for stale in entries[MAX_SAVED_LISTS:]:
            self.delete(stale.list_id)

    def load(self, list_id: str) -> SavedList | None:
        path = self._record_path(list_id)
        if not path.exists():
            return None
        try:
            return SavedList.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return None

    def read_csv(self, list_id: str) -> bytes | None:
        """Return the stored upload exactly as it arrived."""
        path = self._csv_path(list_id)
        return path.read_bytes() if path.exists() else None

    def list_all(self) -> list[SavedList]:
        """Saved lists, most recently used first."""
        entries = []
        for path in self._directory.glob("*.json"):
            try:
                entries.append(SavedList.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(entries, key=lambda item: item.last_used_at or item.saved_at, reverse=True)

    def record_edits(self, list_id: str, *, edits: dict[int, dict[str, str]], removed: list[int]) -> SavedList | None:
        """Store the cells that differ from the uploaded file.

        Whatever the caller sends is stored, so the caller is responsible for sending
        only cells that actually differ.
        """
        entry = self.load(list_id)
        if entry is None:
            return None
        for index, changes in edits.items():
            kept = {key: value for key, value in changes.items() if key in EDITABLE_FIELDS}
            if kept:
                entry.edits.setdefault(str(index), {}).update(kept)
        for index in removed:
            if index not in entry.removed:
                entry.removed.append(index)
        entry.last_used_at = _utc_now()
        self._save_record(entry)
        return entry

    def clear_edits(self, list_id: str) -> SavedList | None:
        """Forget the edits, leaving the stored upload as the only content."""
        entry = self.load(list_id)
        if entry is None:
            return None
        entry.edits = {}
        entry.removed = []
        self._save_record(entry)
        return entry

    def touch(self, list_id: str) -> None:
        """Mark a list as used now, so it sorts to the top."""
        entry = self.load(list_id)
        if entry is not None:
            entry.last_used_at = _utc_now()
            self._save_record(entry)

    def delete(self, list_id: str) -> bool:
        """Remove a saved list and its stored CSV."""
        record = self._record_path(list_id)
        if not record.exists():
            return False
        record.unlink()
        self._csv_path(list_id).unlink(missing_ok=True)
        return True


def apply_saved_edits(contacts, entry: SavedList) -> tuple[list, dict[int, list[str]]]:
    """Replay saved edits onto freshly parsed contacts.

    :param contacts: contacts parsed from the stored CSV, in file order
    :param entry: the saved list holding edits and removals
    :returns: the kept contacts and, per kept index, which fields were edited
    """
    edited_fields: dict[int, list[str]] = {}
    for index_text, changes in entry.edits.items():
        index = int(index_text) if index_text.isdigit() else -1
        if not 0 <= index < len(contacts):
            continue
        for name, value in changes.items():
            if name in EDITABLE_FIELDS:
                setattr(contacts[index], name, value)
                contacts[index].extra[name] = value
                edited_fields.setdefault(index, []).append(name)

    removed = set(entry.removed)
    kept = []
    kept_edits: dict[int, list[str]] = {}
    for index, contact in enumerate(contacts):
        if index in removed:
            continue
        if index in edited_fields:
            kept_edits[len(kept)] = edited_fields[index]
        kept.append(contact)
    return kept, kept_edits
