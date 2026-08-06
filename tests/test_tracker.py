"""Spreadsheet rows, saved field values, and message state precedence."""

from __future__ import annotations

from app.contacts import Contact
from app.history import PriorContact
from app.mailbox import ScheduledMessage
from app.store import DraftRecord
from app.tracker import (
    COLUMNS,
    MessageState,
    TrackerFields,
    build_rows,
    client_from_subject,
    merge_states,
    to_tsv,
)

SUBJECT = "[Harvard Product Lab x Google] Fall 26 Case Collaboration"


def make_contact(**overrides) -> Contact:
    values = {
        "email": "ada@engines.example",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "company": "Analytical Engines",
        "title": "VP of Engineering",
        "extra": {"person_linkedin_url": "https://linkedin.com/in/ada"},
    }
    values.update(overrides)
    return Contact(**values)


def test_the_columns_match_the_spreadsheet_order():
    assert COLUMNS == (
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


def test_the_client_is_read_out_of_the_subject():
    assert client_from_subject(SUBJECT) == "Google"
    assert client_from_subject("[Harvard Product Lab x Schneider Electric] Fall 26") == "Schneider Electric"
    assert client_from_subject("no client here") == ""


def test_a_row_is_filled_from_the_csv_and_the_mailbox(paths):
    states = {
        "ada@engines.example": MessageState(status="scheduled", subject=SUBJECT, when="2026-08-05", message_keys={"m1"})
    }
    saved = TrackerFields(paths.tracker_file)

    row = build_rows([make_contact()], states=states, saved=saved, default_assignee="Bonnie")[0]

    assert row.client == "Google"
    assert row.status == "Scheduled"
    assert row.name == "Ada Lovelace"
    assert row.title == "VP of Engineering"
    assert row.linkedin == "https://linkedin.com/in/ada"
    assert row.re_emailed == "No"
    assert row.assignee == "Bonnie"
    assert row.notes == ""
    # The sheet writes dates as M/D/YYYY, not ISO.
    assert row.last_contact == "8/5/2026"


def test_notes_is_the_only_column_left_blank_once_an_assignee_is_given(paths):
    # Assignee has no CSV source, so it comes from the default or a saved value.
    states = {"ada@engines.example": MessageState(status="draft", subject=SUBJECT, when="2026-08-05")}
    row = build_rows(
        [make_contact()], states=states, saved=TrackerFields(paths.tracker_file), default_assignee="Bonnie"
    )[0]

    blanks = [name for name, value in zip(COLUMNS, row.as_cells(), strict=True) if not value]

    assert blanks == ["Notes"]


def test_a_contact_with_no_message_still_produces_a_full_row(paths):
    row = build_rows([make_contact()], states={}, saved=TrackerFields(paths.tracker_file))[0]

    assert len(row.as_cells()) == len(COLUMNS)
    assert row.status == ""
    assert row.client == "Analytical Engines"


def test_the_client_falls_back_to_the_csv_company(paths):
    states = {"ada@engines.example": MessageState(status="draft", subject="No client tag", when="2026-08-05")}
    row = build_rows([make_contact()], states=states, saved=TrackerFields(paths.tracker_file))[0]

    assert row.client == "Analytical Engines"


def test_saved_values_are_remembered_and_win(paths):
    saved = TrackerFields(paths.tracker_file)
    saved.update("Ada@Engines.Example", {"assignee": "Bonnie C", "notes": "warm intro"})
    saved.save()

    reloaded = TrackerFields(paths.tracker_file)
    row = build_rows([make_contact()], states={}, saved=reloaded, default_assignee="Someone Else")[0]

    assert row.assignee == "Bonnie C"
    assert row.notes == "warm intro"


def test_an_unexpected_key_is_not_stored(paths):
    saved = TrackerFields(paths.tracker_file)
    saved.update("ada@engines.example", {"assignee": "B", "unexpected": "x"})

    assert "unexpected" not in saved.get("ada@engines.example")


def test_clearing_a_value_is_kept(paths):
    saved = TrackerFields(paths.tracker_file)
    saved.update("ada@engines.example", {"notes": "first"})
    saved.update("ada@engines.example", {"notes": ""})

    assert saved.get("ada@engines.example")["notes"] == ""


def test_tsv_has_a_header_and_one_line_per_row(paths):
    rows = build_rows(
        [make_contact(), make_contact(email="grace@compilers.example", first_name="Grace")],
        states={},
        saved=TrackerFields(paths.tracker_file),
    )
    lines = to_tsv(rows).split("\n")

    assert lines[0] == "\t".join(COLUMNS)
    assert len(lines) == 3
    assert all(len(line.split("\t")) == len(COLUMNS) for line in lines)


def test_tabs_and_newlines_in_a_value_cannot_break_the_columns(paths):
    saved = TrackerFields(paths.tracker_file)
    saved.update("ada@engines.example", {"notes": "line one\nline two\tafter a tab"})
    rows = build_rows([make_contact()], states={}, saved=saved)

    line = to_tsv(rows).split("\n")[1]

    assert len(line.split("\t")) == len(COLUMNS)
    assert "line one line two after a tab" in line


def test_scheduled_beats_draft_and_sent_beats_both():
    batch = _batch_with(DraftRecord("d1", "m1", "ada@engines.example", SUBJECT))
    scheduled = {
        "ada@engines.example": ScheduledMessage(
            message_id="m1", to="ada@engines.example", subject=SUBJECT, send_at="Wed, 5 Aug 2026 17:00:00 -0700"
        )
    }

    states = merge_states(scheduled=scheduled, live_draft_ids={"d1"}, batches=[batch])
    assert states["ada@engines.example"].status == "scheduled"

    sent = PriorContact(email="ada@engines.example", source="gmail_sent", first_contact="2026-08-05", message_count=1)
    states = merge_states(
        scheduled=scheduled, live_draft_ids={"d1"}, batches=[batch], sent_lookup=lambda _address: sent
    )
    assert states["ada@engines.example"].status == "sent"


def test_a_draft_gone_from_gmail_reads_as_sent_once_sent_mail_confirms_it():
    batch = _batch_with(DraftRecord("d1", "m1", "ada@engines.example", SUBJECT))
    sent = PriorContact(email="ada@engines.example", source="gmail_sent", first_contact="2026-08-05", message_count=1)

    without = merge_states(scheduled={}, live_draft_ids=set(), batches=[batch])
    withsent = merge_states(scheduled={}, live_draft_ids=set(), batches=[batch], sent_lookup=lambda _address: sent)

    assert without["ada@engines.example"].status == "deleted"
    assert withsent["ada@engines.example"].status == "sent"


def test_a_deleted_draft_does_not_count_as_a_round_of_contact():
    deleted = DraftRecord("d1", "m1", "ada@engines.example", SUBJECT)
    deleted.deleted_at = "2026-08-04T00:00:00+00:00"
    live = DraftRecord("d2", "m2", "ada@engines.example", SUBJECT)

    states = merge_states(scheduled={}, live_draft_ids={"d2"}, batches=[_batch_with(deleted, live)])

    assert states["ada@engines.example"].re_emailed == "No"


def test_two_sent_messages_mark_the_contact_as_re_emailed():
    batch = _batch_with(DraftRecord("d1", "m1", "ada@engines.example", SUBJECT))
    twice = PriorContact(
        email="ada@engines.example",
        source="gmail_sent",
        first_contact="2026-06-01",
        last_contact="2026-08-05",
        message_count=2,
    )

    states = merge_states(scheduled={}, live_draft_ids={"d1"}, batches=[batch], sent_lookup=lambda _address: twice)

    assert states["ada@engines.example"].re_emailed == "Yes"


def _batch_with(*drafts):
    """A batch record holding the given drafts."""

    class Batch:
        batch_id = "b1"
        created_at = "2026-08-05T00:00:00+00:00"

    batch = Batch()
    batch.drafts = list(drafts)
    return batch
