"""Rendering contacts into messages and turning them into Gmail drafts."""

from __future__ import annotations

from dataclasses import dataclass, field

from .contacts import Contact
from .gmail_client import GmailDraftService, GmailError
from .history import ContactGuard
from .message import Attachment, build_message
from .store import Batch, BatchStore, DraftRecord
from .templating import build_context, find_placeholders, render

# Gmail API failures with nothing created that mean the run should stop, since the
# token or the quota is usually gone. Message build failures do not count toward it.
GIVE_UP_AFTER_FAILURES = 5


@dataclass
class RenderedDraft:
    """One message rendered for one contact, before it reaches Gmail."""

    contact: Contact
    subject: str
    body: str
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


@dataclass
class TemplateSet:
    """The templates and options shared by every draft in a batch."""

    subject_template: str
    body_template: str
    signoff_template: str = ""
    sender_name: str = ""
    cc: str = ""
    bcc: str = ""
    send_as_html: bool = False
    use_markdown: bool = False


def render_one(contact: Contact, templates: TemplateSet) -> RenderedDraft:
    """Render subject and body for a single contact.

    The sign off is rendered first so its own placeholders resolve before the body
    uses it. It is appended to the end of the body unless the body already places
    it with a signoff placeholder, which would otherwise repeat it.
    """
    context = build_context(contact.as_context(), templates.sender_name, signoff="")
    signoff = render(templates.signoff_template, context)
    context["signoff"] = signoff.text

    subject = render(templates.subject_template, context)
    body = render(templates.body_template, context)

    body_text = body.text
    body_places_signoff = "signoff" in find_placeholders(templates.body_template)
    if signoff.text.strip() and not body_places_signoff:
        separator = "" if body_text.endswith("\n\n") else ("\n" if body_text.endswith("\n") else "\n\n")
        body_text = f"{body_text}{separator}{signoff.text}"

    missing: list[str] = []
    for result in (subject, body, signoff):
        for key in result.missing:
            if key not in missing:
                missing.append(key)

    return RenderedDraft(contact=contact, subject=subject.text, body=body_text, missing=missing)


def render_all(contacts: list[Contact], templates: TemplateSet) -> list[RenderedDraft]:
    """Render every contact in order."""
    return [render_one(contact, templates) for contact in contacts]


@dataclass
class CreateOutcome:
    """Result of one create run."""

    batch: Batch
    created: int = 0
    failed: int = 0
    skipped: int = 0
    blocked: int = 0
    stopped_early: bool = False
    checked_sent_mail: bool = False
    sent_check_errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def history_report(self) -> dict[str, object]:
        """Summary of addresses held back because they were contacted before."""
        blocked = self.batch.blocked
        by_source: dict[str, int] = {}
        for entry in blocked:
            source = str(entry.get("source", "unknown"))
            by_source[source] = by_source.get(source, 0) + 1
        return {
            "blocked": blocked,
            "blocked_count": len(blocked),
            "by_source": by_source,
            "checked_sent_mail": self.checked_sent_mail,
            "sent_check_errors": self.sent_check_errors,
            "coverage_complete": self.checked_sent_mail and not self.sent_check_errors,
        }


def create_drafts(
    *,
    service: GmailDraftService,
    store: BatchStore,
    contacts: list[Contact],
    templates: TemplateSet,
    attachments: list[Attachment],
    source_name: str,
    skip_incomplete: bool = True,
    guard: ContactGuard | None = None,
    save_every: int = 10,
) -> CreateOutcome:
    """Render each contact and create a Gmail draft for it.

    The batch is saved as drafts are created, not only at the end, so an
    interrupted run still leaves a record that can be used to delete what it made.

    :param service: authorized Gmail draft service
    :param store: where the batch record is written
    :param contacts: recipients to draft for
    :param templates: shared subject, body, sign off, and options
    :param attachments: files attached to every draft in the batch
    :param source_name: label for the batch, usually the CSV filename
    :param skip_incomplete: skip rows with unresolved placeholders instead of
        creating a draft containing raw template syntax
    :param guard: prior contact check; a blocked address is never drafted
    :param save_every: how many contacts to process between saves
    :returns: the batch record and per row counts
    """
    batch = store.new_batch(
        source_name=source_name,
        subject_template=templates.subject_template,
        attachment_names=[item.filename for item in attachments],
    )
    outcome = CreateOutcome(batch=batch)
    store.save(batch)

    for index, rendered in enumerate(render_all(contacts, templates), start=1):
        # Prior contact is checked before anything is built, so a blocked address
        # never reaches Gmail even if the template or attachments are wrong.
        if guard is not None:
            prior = guard.check(rendered.contact.email)
            if prior is not None:
                batch.blocked.append(prior.to_dict())
                outcome.blocked += 1
                continue

        if skip_incomplete and not rendered.ok:
            batch.skipped.append(
                {
                    "email": rendered.contact.email,
                    "reason": "unresolved placeholders: " + ", ".join(rendered.missing),
                }
            )
            outcome.skipped += 1
            continue

        try:
            message = build_message(
                to=rendered.contact.email,
                subject=rendered.subject,
                body=rendered.body,
                cc=templates.cc,
                bcc=templates.bcc,
                attachments=attachments,
                # Markdown implies an HTML part, or the formatting would be lost.
                as_html=templates.send_as_html or templates.use_markdown,
                as_markdown=templates.use_markdown,
            )
        except ValueError as error:
            # One malformed row should not abort the rest of the batch.
            batch.failures.append({"email": rendered.contact.email, "error": f"could not build message: {error}"})
            outcome.failed += 1
            continue

        try:
            reference = service.create_draft(message, to=rendered.contact.email, subject=rendered.subject)
        except GmailError as error:
            batch.failures.append({"email": rendered.contact.email, "error": str(error)})
            outcome.failed += 1
            # Repeated failures usually mean the token or quota is gone, so give up
            # rather than burning through the rest of the list.
            if outcome.failed >= GIVE_UP_AFTER_FAILURES and outcome.created == 0:
                outcome.stopped_early = True
                break
            continue

        batch.drafts.append(
            DraftRecord(
                draft_id=reference.draft_id,
                message_id=reference.message_id,
                to=reference.to,
                subject=reference.subject,
            )
        )
        outcome.created += 1

        if index % save_every == 0:
            store.save(batch)

    if guard is not None:
        outcome.checked_sent_mail = guard.checked_sent_mail
        outcome.sent_check_errors = list(guard.sent_errors)

    store.save(batch)
    return outcome


@dataclass
class DeleteOutcome:
    """Result of one delete run."""

    deleted: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)


def delete_drafts(
    *, service: GmailDraftService, store: BatchStore, batch: Batch, draft_ids: list[str] | None = None
) -> DeleteOutcome:
    """Delete drafts from a batch.

    :param service: authorized Gmail draft service
    :param store: where the updated batch record is written
    :param batch: the batch the drafts belong to
    :param draft_ids: specific drafts to delete, or None for every live draft
    :returns: how many were deleted and any that failed
    """
    live = {draft.draft_id for draft in batch.drafts if draft.deleted_at is None}
    targets = live if draft_ids is None else live.intersection(draft_ids)

    outcome = DeleteOutcome()
    removed: set[str] = set()
    for draft_id in sorted(targets):
        try:
            service.delete_draft(draft_id)
        except GmailError as error:
            outcome.failures.append({"draft_id": draft_id, "error": str(error)})
            continue
        removed.add(draft_id)
        outcome.deleted += 1

    if removed:
        store.mark_deleted(batch, removed)
    return outcome
