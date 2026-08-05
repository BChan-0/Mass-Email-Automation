"""Rendering, draft creation, and deletion against the Gmail stub."""

from __future__ import annotations

import email

from app.contacts import Contact, parse_csv
from app.drafts import TemplateSet, create_drafts, delete_drafts, render_one
from app.history import (
    SOURCE_PRIOR_BATCH,
    SOURCE_SENT_MAIL,
    SOURCE_SUPPRESSION,
    ContactGuard,
    HistoryIndex,
    SentMailChecker,
    build_history_index,
)
from app.message import Attachment


def make_templates(**overrides) -> TemplateSet:
    values = {
        "subject_template": "Question about {{company}}",
        "body_template": "Hi {{first_name|there}},\n\nAbout your work as {{title}}.",
        "signoff_template": "Best,\n{{sender_name}}",
        "sender_name": "Bonnie",
    }
    values.update(overrides)
    return TemplateSet(**values)


def test_signoff_is_appended_with_a_blank_line():
    contact = Contact(email="ada@engines.example", first_name="Ada", company="Engines", title="VP")
    rendered = render_one(contact, make_templates())

    assert rendered.subject == "Question about Engines"
    assert rendered.body.endswith("Best,\nBonnie")
    assert "About your work as VP.\n\nBest," in rendered.body
    assert rendered.ok


def test_body_can_place_the_signoff_itself_without_repeating_it():
    contact = Contact(email="ada@engines.example", first_name="Ada")
    templates = make_templates(
        subject_template="Hello",
        body_template="Hi {{first_name}},\n\n{{signoff}}\n\nP.S. one more thing",
        signoff_template="Warmly,\n{{sender_name}}",
    )
    rendered = render_one(contact, templates)

    assert rendered.body.count("Warmly,\nBonnie") == 1
    assert rendered.body.endswith("P.S. one more thing")
    assert rendered.ok


def test_missing_field_is_reported_per_contact():
    contact = Contact(email="alan@bletchley.example", first_name="Alan", company="Bletchley")
    rendered = render_one(contact, make_templates())

    assert rendered.missing == ["title"]
    assert not rendered.ok


def test_create_drafts_records_a_batch(gmail, store, apollo_csv):
    contacts = parse_csv(apollo_csv, max_contacts=100).contacts
    outcome = create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(),
        attachments=[],
        source_name="apollo.csv",
    )

    # Alan has no title, so he is skipped by default.
    assert outcome.created == 2
    assert outcome.skipped == 1
    assert outcome.failed == 0

    saved = store.load(outcome.batch.batch_id)
    assert saved.live_count == 2
    assert saved.source_name == "apollo.csv"
    assert "unresolved placeholders: title" in saved.skipped[0]["reason"]


def test_allowing_incomplete_rows_drafts_every_contact(gmail, store, apollo_csv):
    contacts = parse_csv(apollo_csv, max_contacts=100).contacts
    outcome = create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(),
        attachments=[],
        source_name="apollo.csv",
        skip_incomplete=False,
    )

    assert outcome.created == 3
    assert outcome.skipped == 0


def test_attachment_is_included_in_every_draft(gmail, store):
    contacts = [Contact(email="ada@engines.example", first_name="Ada", company="Engines", title="VP")]
    attachment = Attachment(filename="deck.pdf", content=b"%PDF-1.4 fake", mime_type="application/pdf")

    outcome = create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(),
        attachments=[attachment],
        source_name="one.csv",
    )

    assert outcome.created == 1
    assert outcome.batch.attachment_names == ["deck.pdf"]
    raw = next(iter(gmail.drafts.values()))["raw"]
    parsed = email.message_from_bytes(raw)
    names = [part.get_filename() for part in parsed.walk() if part.get_filename()]
    assert names == ["deck.pdf"]


def test_cc_and_bcc_reach_the_message(gmail, store):
    contacts = [Contact(email="ada@engines.example", first_name="Ada", company="Engines", title="VP")]
    create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(cc="team@example.com", bcc="log@example.com"),
        attachments=[],
        source_name="one.csv",
    )

    parsed = email.message_from_bytes(next(iter(gmail.drafts.values()))["raw"])
    assert parsed["Cc"] == "team@example.com"
    assert parsed["Bcc"] == "log@example.com"
    assert parsed["To"] == "ada@engines.example"


def test_failures_are_recorded_and_the_run_continues(gmail, store):
    contacts = [
        Contact(email="ada@engines.example", first_name="Ada", company="Engines", title="VP"),
        Contact(email="grace@compilers.example", first_name="Grace", company="Compilers", title="Chief"),
    ]
    gmail.fail_on = {"ada@engines.example"}

    outcome = create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(),
        attachments=[],
        source_name="two.csv",
    )

    assert outcome.created == 1
    assert outcome.failed == 1
    assert outcome.batch.failures[0]["email"] == "ada@engines.example"


def test_a_row_that_cannot_build_a_message_is_recorded_not_fatal(gmail, store):
    contacts = [
        Contact(email="bad\r\nrow@example.com", first_name="Bad", company="C", title="T"),
        Contact(email="ada@engines.example", first_name="Ada", company="Engines", title="VP"),
    ]

    outcome = create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(),
        attachments=[],
        source_name="mixed.csv",
    )

    # The sanitized address still drafts, so both rows go through rather than crash.
    assert outcome.created == 2
    assert outcome.failed == 0


def test_repeated_failures_stop_the_run(gmail, store):
    contacts = [
        Contact(email=f"person{index}@example.com", first_name="P", company="C", title="T") for index in range(20)
    ]
    gmail.fail_on = {contact.email for contact in contacts}

    outcome = create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(),
        attachments=[],
        source_name="many.csv",
    )

    assert outcome.stopped_early
    assert outcome.created == 0
    assert outcome.failed == 5


def test_a_previously_emailed_contact_is_never_drafted(gmail, store):
    gmail.sent_to["grace@compilers.example"] = ["Tue, 3 Mar 2026 10:00:00 -0800"]
    contacts = [
        Contact(email="ada@engines.example", first_name="Ada", company="Engines", title="VP"),
        Contact(email="grace@compilers.example", first_name="Grace", company="Compilers", title="Chief"),
    ]

    outcome = create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(),
        attachments=[],
        source_name="two.csv",
        guard=ContactGuard(sent_checker=SentMailChecker(gmail)),
    )

    assert outcome.created == 1
    assert outcome.blocked == 1
    assert [draft.to for draft in outcome.batch.drafts] == ["ada@engines.example"]
    blocked = outcome.batch.blocked[0]
    assert blocked["email"] == "grace@compilers.example"
    assert blocked["first_contact"] == "2026-03-03"
    assert blocked["source"] == SOURCE_SENT_MAIL


def test_a_second_run_of_the_same_list_blocks_everyone(gmail, store):
    contacts = [Contact(email="ada@engines.example", first_name="Ada", company="Engines", title="VP")]
    first = create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(),
        attachments=[],
        source_name="run1.csv",
        guard=ContactGuard(sent_checker=SentMailChecker(gmail)),
    )
    assert first.created == 1

    second = create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(),
        attachments=[],
        source_name="run2.csv",
        guard=ContactGuard(history=build_history_index(store.list_batches())),
    )

    assert second.created == 0
    assert second.blocked == 1
    assert second.batch.blocked[0]["source"] == SOURCE_PRIOR_BATCH


def test_deleting_the_first_run_lets_the_same_list_run_again(gmail, store):
    contacts = [Contact(email="ada@engines.example", first_name="Ada", company="Engines", title="VP")]
    first = create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(),
        attachments=[],
        source_name="run1.csv",
        guard=ContactGuard(sent_checker=SentMailChecker(gmail)),
    )
    delete_drafts(service=gmail, store=store, batch=first.batch)

    second = create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(),
        attachments=[],
        source_name="run2.csv",
        guard=ContactGuard(
            history=build_history_index(store.list_batches(), live_draft_ids=gmail.list_draft_ids()),
            sent_checker=SentMailChecker(gmail),
        ),
    )

    assert second.created == 1
    assert second.blocked == 0


def test_the_report_groups_blocks_by_source(gmail, store):
    gmail.sent_to["grace@compilers.example"] = ["Tue, 3 Mar 2026 10:00:00 -0800"]
    index = HistoryIndex()
    index.record("ada@engines.example", "2026-01-01T00:00:00+00:00")
    contacts = [
        Contact(email="ada@engines.example", first_name="Ada", company="Engines", title="VP"),
        Contact(email="grace@compilers.example", first_name="Grace", company="Compilers", title="Chief"),
        Contact(email="katherine@orbital.example", first_name="Katherine", company="Orbital", title="Dir"),
        Contact(email="new@example.org", first_name="New", company="Fresh", title="Lead"),
    ]

    outcome = create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(),
        attachments=[],
        source_name="mixed.csv",
        guard=ContactGuard(
            history=index,
            suppression={"katherine@orbital.example": "asked to be removed"},
            sent_checker=SentMailChecker(gmail),
        ),
    )
    report = outcome.history_report

    assert outcome.created == 1
    assert report["blocked_count"] == 3
    assert report["by_source"] == {
        SOURCE_PRIOR_BATCH: 1,
        SOURCE_SENT_MAIL: 1,
        SOURCE_SUPPRESSION: 1,
    }
    assert report["coverage_complete"] is True


def test_the_report_says_coverage_was_partial_after_a_lookup_failure(gmail, store):
    gmail.search_failures.add("ada@engines.example")
    contacts = [Contact(email="ada@engines.example", first_name="Ada", company="Engines", title="VP")]

    outcome = create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(),
        attachments=[],
        source_name="one.csv",
        guard=ContactGuard(sent_checker=SentMailChecker(gmail)),
    )

    # The draft is still created; the report says the check could not confirm it.
    assert outcome.created == 1
    assert outcome.history_report["coverage_complete"] is False
    assert len(outcome.history_report["sent_check_errors"]) == 1


def test_blocked_contacts_survive_a_reload(gmail, store):
    gmail.sent_to["grace@compilers.example"] = ["Tue, 3 Mar 2026 10:00:00 -0800"]
    contacts = [Contact(email="grace@compilers.example", first_name="Grace", company="C", title="T")]

    outcome = create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(),
        attachments=[],
        source_name="one.csv",
        guard=ContactGuard(sent_checker=SentMailChecker(gmail)),
    )
    reloaded = store.load(outcome.batch.batch_id)

    assert reloaded.blocked[0]["email"] == "grace@compilers.example"
    assert reloaded.blocked[0]["first_contact"] == "2026-03-03"


def test_delete_all_drafts_in_a_batch(gmail, store):
    contacts = [
        Contact(email="ada@engines.example", first_name="Ada", company="Engines", title="VP"),
        Contact(email="grace@compilers.example", first_name="Grace", company="Compilers", title="Chief"),
    ]
    batch = create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(),
        attachments=[],
        source_name="two.csv",
    ).batch

    outcome = delete_drafts(service=gmail, store=store, batch=batch)

    assert outcome.deleted == 2
    assert gmail.drafts == {}
    reloaded = store.load(batch.batch_id)
    assert reloaded.live_count == 0
    assert reloaded.deleted_count == 2


def test_delete_only_the_selected_drafts(gmail, store):
    contacts = [
        Contact(email="ada@engines.example", first_name="Ada", company="Engines", title="VP"),
        Contact(email="grace@compilers.example", first_name="Grace", company="Compilers", title="Chief"),
    ]
    batch = create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(),
        attachments=[],
        source_name="two.csv",
    ).batch
    keep, remove = batch.drafts[0].draft_id, batch.drafts[1].draft_id

    outcome = delete_drafts(service=gmail, store=store, batch=batch, draft_ids=[remove])

    assert outcome.deleted == 1
    assert keep in gmail.drafts
    assert remove not in gmail.drafts


def test_deleting_twice_is_a_no_op(gmail, store):
    contacts = [Contact(email="ada@engines.example", first_name="Ada", company="Engines", title="VP")]
    batch = create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(),
        attachments=[],
        source_name="one.csv",
    ).batch

    delete_drafts(service=gmail, store=store, batch=batch)
    second = delete_drafts(service=gmail, store=store, batch=store.load(batch.batch_id))

    assert second.deleted == 0
    assert second.failures == []


def test_unknown_draft_ids_are_ignored(gmail, store):
    contacts = [Contact(email="ada@engines.example", first_name="Ada", company="Engines", title="VP")]
    batch = create_drafts(
        service=gmail,
        store=store,
        contacts=contacts,
        templates=make_templates(),
        attachments=[],
        source_name="one.csv",
    ).batch

    outcome = delete_drafts(service=gmail, store=store, batch=batch, draft_ids=["not-in-this-batch"])

    assert outcome.deleted == 0
    assert len(gmail.drafts) == 1
