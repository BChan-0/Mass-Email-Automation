"""Shared fixtures and a Gmail stub so tests never call the real API."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Paths  # noqa: E402
from app.gmail_client import DraftRef, GmailError  # noqa: E402
from app.store import BatchStore  # noqa: E402


@dataclass
class FakeGmail:
    """Stand in for GmailDraftService that records calls in memory.

    ``fail_on`` holds addresses that should raise, so failure handling can be
    tested without a network. ``sent_to`` maps an address to the dates it was
    emailed, standing in for what a sent mail search would return.
    """

    drafts: dict[str, dict] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    fail_on: set[str] = field(default_factory=set)
    sent_to: dict[str, list[str]] = field(default_factory=dict)
    search_failures: set[str] = field(default_factory=set)
    searches: list[str] = field(default_factory=list)
    list_drafts_fails: bool = False
    counter: int = 0

    def profile_email(self) -> str:
        return "tester@example.com"

    def list_draft_ids(self) -> set[str]:
        """Ids of drafts that still exist, mirroring what Gmail would report."""
        if self.list_drafts_fails:
            raise GmailError("could not list drafts: stubbed failure")
        return set(self.drafts)

    def search_sent(self, query: str, *, limit: int = 20) -> list[dict]:
        """Return metadata format messages for any address named in the query."""
        self.searches.append(query)
        address = query.replace("in:sent", "").replace("to:", "").strip().lower()
        if address in self.search_failures:
            raise GmailError(f"could not search sent mail: stubbed failure for {address}")

        messages = []
        for date in self.sent_to.get(address, []):
            messages.append(
                {
                    "id": f"msg-{address}-{date}",
                    "internalDate": "0",
                    "payload": {
                        "headers": [
                            {"name": "To", "value": address},
                            {"name": "Date", "value": date},
                        ]
                    },
                }
            )
        return messages

    def create_draft(self, message, *, to: str, subject: str) -> DraftRef:
        if to in self.fail_on:
            raise GmailError(f"could not create draft for {to}: stubbed failure")
        self.counter += 1
        draft_id = f"draft-{self.counter}"
        self.drafts[draft_id] = {"to": to, "subject": subject, "raw": message.as_bytes()}
        return DraftRef(draft_id=draft_id, message_id=f"msg-{self.counter}", to=to, subject=subject)

    def delete_draft(self, draft_id: str) -> None:
        if draft_id in self.fail_on:
            raise GmailError(f"could not delete draft {draft_id}: stubbed failure")
        self.drafts.pop(draft_id, None)
        self.deleted.append(draft_id)


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    """A Paths rooted in a temporary directory."""
    layout = Paths(root=tmp_path)
    layout.ensure()
    return layout


@pytest.fixture
def store(paths: Paths) -> BatchStore:
    return BatchStore(paths.batches)


@pytest.fixture
def gmail() -> FakeGmail:
    return FakeGmail()


@pytest.fixture
def apollo_csv() -> str:
    """A CSV shaped like an Apollo export, including rows that should be dropped."""
    return (
        "First Name,Last Name,Title,Company,Email,Seniority\n"
        "Ada,Lovelace,VP Engineering,Analytical Engines,ada@engines.example,vp\n"
        "Grace,Hopper,Chief Scientist,Compiler Works,grace@compilers.example,c_suite\n"
        "Alan,Turing,,Bletchley Labs,alan@bletchley.example,director\n"
        "Locked,Contact,Engineer,Hidden Co,email_not_unlocked,manager\n"
        "Ada,Lovelace,VP Engineering,Analytical Engines,ada@engines.example,vp\n"
        "NoEmail,Person,Analyst,Nowhere Inc,,analyst\n"
    )
