"""JSON persistence for saved settings and draft batch records.

Batches record which draft ids a run created so the same run can be deleted later
without touching drafts written by hand.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SETTINGS: dict[str, object] = {
    "sender_name": "",
    "subject_template": "Quick question about {{company}}",
    "body_template": (
        "Hi {{first_name|there}},\n\n"
        "I came across your work as {{title}} at {{company}} and wanted to reach out.\n\n"
        "Would you be open to a short conversation next week?\n"
    ),
    "signoff_template": "Best,\n{{sender_name}}",
    "cc": "",
    "bcc": "",
    "send_as_html": False,
    "use_markdown": False,
    "skip_incomplete": True,
    "skip_previously_emailed": True,
}


def _utc_now() -> str:
    """Current time as an ISO 8601 string in UTC."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, payload: object) -> None:
    """Write JSON atomically so a crash cannot leave a truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def load_settings(path: Path) -> dict[str, object]:
    """Read saved settings, filling in defaults for anything absent."""
    settings = dict(DEFAULT_SETTINGS)
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return settings
        if isinstance(saved, dict):
            settings.update({key: saved[key] for key in DEFAULT_SETTINGS if key in saved})
    return settings


def save_settings(path: Path, values: dict[str, object]) -> dict[str, object]:
    """Merge ``values`` into saved settings, ignoring unknown keys."""
    settings = load_settings(path)
    settings.update({key: values[key] for key in DEFAULT_SETTINGS if key in values})
    _write_json(path, settings)
    return settings


@dataclass
class DraftRecord:
    """One draft that was created, and whether it has since been deleted."""

    draft_id: str
    message_id: str
    to: str
    subject: str
    deleted_at: str | None = None


@dataclass
class Batch:
    """One draft creation run."""

    batch_id: str
    created_at: str
    source_name: str
    subject_template: str
    attachment_names: list[str] = field(default_factory=list)
    drafts: list[DraftRecord] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    # Addresses held back because they had been contacted before.
    blocked: list[dict[str, object]] = field(default_factory=list)

    @property
    def live_count(self) -> int:
        return sum(1 for draft in self.drafts if draft.deleted_at is None)

    @property
    def deleted_count(self) -> int:
        return sum(1 for draft in self.drafts if draft.deleted_at is not None)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["live_count"] = self.live_count
        payload["deleted_count"] = self.deleted_count
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> Batch:
        drafts = [
            DraftRecord(
                draft_id=item.get("draft_id", ""),
                message_id=item.get("message_id", ""),
                to=item.get("to", ""),
                subject=item.get("subject", ""),
                deleted_at=item.get("deleted_at"),
            )
            for item in payload.get("drafts", [])
        ]
        return cls(
            batch_id=payload.get("batch_id", ""),
            created_at=payload.get("created_at", ""),
            source_name=payload.get("source_name", ""),
            subject_template=payload.get("subject_template", ""),
            attachment_names=list(payload.get("attachment_names", [])),
            drafts=drafts,
            failures=list(payload.get("failures", [])),
            skipped=list(payload.get("skipped", [])),
            blocked=list(payload.get("blocked", [])),
        )


class BatchStore:
    """Batch records on disk, one JSON file per batch."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _path(self, batch_id: str) -> Path:
        # Batch ids arrive straight from URL segments, so this guard is the only thing
        # keeping a crafted id from escaping the batches directory.
        if not batch_id or "/" in batch_id or "\\" in batch_id or batch_id.startswith("."):
            raise ValueError(f"invalid batch id: {batch_id!r}")
        return self._directory / f"{batch_id}.json"

    def new_batch(self, *, source_name: str, subject_template: str, attachment_names: list[str]) -> Batch:
        """Create an unsaved batch with a fresh id."""
        return Batch(
            batch_id=uuid.uuid4().hex[:12],
            created_at=_utc_now(),
            source_name=source_name,
            subject_template=subject_template,
            attachment_names=attachment_names,
        )

    def save(self, batch: Batch) -> None:
        _write_json(self._path(batch.batch_id), batch.to_dict())

    def load(self, batch_id: str) -> Batch | None:
        path = self._path(batch_id)
        if not path.exists():
            return None
        try:
            return Batch.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return None

    def list_batches(self) -> list[Batch]:
        """All batches, newest first."""
        batches = []
        for path in self._directory.glob("*.json"):
            try:
                batches.append(Batch.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(batches, key=lambda batch: batch.created_at, reverse=True)

    def mark_deleted(self, batch: Batch, draft_ids: set[str]) -> None:
        """Stamp the given drafts as deleted and save the batch."""
        stamp = _utc_now()
        for draft in batch.drafts:
            if draft.draft_id in draft_ids and draft.deleted_at is None:
                draft.deleted_at = stamp
        self.save(batch)

    def delete_record(self, batch_id: str) -> bool:
        """Remove a batch record from disk. Does not touch Gmail."""
        path = self._path(batch_id)
        if not path.exists():
            return False
        path.unlink()
        return True
