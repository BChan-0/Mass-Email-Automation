"""Prior contact checks that keep an address out of a batch."""

from __future__ import annotations

import pytest

from app.history import (
    SOURCE_PRIOR_BATCH,
    SOURCE_SENT_MAIL,
    SOURCE_SUPPRESSION,
    ContactGuard,
    HistoryIndex,
    SentMailChecker,
    build_history_index,
    fetch_live_draft_ids,
    load_suppression_list,
    normalize_address,
)
from app.store import DraftRecord


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ada@Engines.Example", "ada@engines.example"),
        ("  ada@engines.example  ", "ada@engines.example"),
        ("Ada Lovelace <ada@engines.example>", "ada@engines.example"),
        ("<ada@engines.example>", "ada@engines.example"),
        ("", ""),
    ],
)
def test_addresses_are_normalized_for_comparison(raw, expected):
    assert normalize_address(raw) == expected


def test_history_index_records_first_and_last_contact():
    index = HistoryIndex()
    index.record("ada@engines.example", "2026-06-01T00:00:00+00:00")
    index.record("Ada@Engines.Example", "2026-02-01T00:00:00+00:00")

    found = index.lookup("ada@engines.example")

    assert found.source == SOURCE_PRIOR_BATCH
    assert found.first_contact == "2026-02-01"
    assert found.last_contact == "2026-06-01"
    assert found.message_count == 2


def test_history_index_returns_none_for_a_new_address():
    assert HistoryIndex().lookup("new@example.org") is None


def test_a_deleted_draft_no_longer_blocks_the_address(store):
    # Deleting a draft means nothing was sent and nothing is pending, so the
    # address is free again.
    batch = store.new_batch(source_name="a.csv", subject_template="s", attachment_names=[])
    batch.created_at = "2026-03-01T00:00:00+00:00"
    batch.drafts.append(DraftRecord("d1", "m1", "ada@engines.example", "s"))
    deleted = DraftRecord("d2", "m2", "grace@compilers.example", "s")
    deleted.deleted_at = "2026-03-02T00:00:00+00:00"
    batch.drafts.append(deleted)
    store.save(batch)

    index = build_history_index(store.list_batches())

    assert index.lookup("ada@engines.example") is not None
    assert index.lookup("grace@compilers.example") is None


def test_a_draft_missing_from_gmail_no_longer_blocks_the_address(store):
    # Deleted or sent straight from Gmail, the local record still says live, so
    # Gmail's own draft list decides.
    batch = store.new_batch(source_name="a.csv", subject_template="s", attachment_names=[])
    batch.drafts.append(DraftRecord("d1", "m1", "ada@engines.example", "s"))
    batch.drafts.append(DraftRecord("d2", "m2", "grace@compilers.example", "s"))
    store.save(batch)

    index = build_history_index(store.list_batches(), live_draft_ids={"d1"})

    assert index.lookup("ada@engines.example") is not None
    assert index.lookup("grace@compilers.example") is None


def test_without_gmail_ids_the_local_record_is_trusted(store):
    batch = store.new_batch(source_name="a.csv", subject_template="s", attachment_names=[])
    batch.drafts.append(DraftRecord("d1", "m1", "ada@engines.example", "s"))
    store.save(batch)

    index = build_history_index(store.list_batches(), live_draft_ids=None)

    assert index.lookup("ada@engines.example") is not None


def test_fetching_live_draft_ids_reports_a_failure(gmail):
    gmail.list_drafts_fails = True

    identifiers, reason = fetch_live_draft_ids(gmail)

    assert identifiers is None
    assert "could not list drafts" in reason


def test_fetching_live_draft_ids_without_a_service():
    identifiers, reason = fetch_live_draft_ids(None)

    assert identifiers is None
    assert reason == "no Gmail connection"


def test_the_run_in_progress_can_be_excluded(store):
    batch = store.new_batch(source_name="a.csv", subject_template="s", attachment_names=[])
    batch.drafts.append(DraftRecord("d1", "m1", "ada@engines.example", "s"))
    store.save(batch)

    index = build_history_index(store.list_batches(), exclude_batch_id=batch.batch_id)

    assert index.lookup("ada@engines.example") is None


def test_suppression_list_parses_notes_and_comments(paths):
    paths.suppression_file.write_text(
        "# people who asked to be left alone\n"
        "ada@engines.example, asked not to be contacted\n"
        "grace@compilers.example\tbounced twice\n"
        # A single space has to separate the note too, or the whole line would be
        # stored as one key that could never match an address.
        "jean@eniac.example moved on\n"
        "Katherine@Orbital.Example\n"
        "\n",
        encoding="utf-8",
    )

    entries = load_suppression_list(paths.suppression_file)

    assert entries["ada@engines.example"] == "asked not to be contacted"
    assert entries["grace@compilers.example"] == "bounced twice"
    assert entries["jean@eniac.example"] == "moved on"
    assert entries["katherine@orbital.example"] == ""
    assert len(entries) == 4


def test_a_missing_suppression_file_is_empty(paths):
    assert load_suppression_list(paths.suppression_file) == {}


def test_sent_mail_reports_the_earliest_contact(gmail):
    gmail.sent_to["grace@compilers.example"] = [
        "Wed, 8 Jul 2026 09:00:00 -0700",
        "Tue, 3 Mar 2026 10:00:00 -0800",
    ]

    found = SentMailChecker(gmail).check("Grace@Compilers.Example")

    assert found.source == SOURCE_SENT_MAIL
    assert found.first_contact == "2026-03-03"
    assert found.last_contact == "2026-07-08"
    assert found.message_count == 2


def test_sent_mail_returns_none_when_never_contacted(gmail):
    assert SentMailChecker(gmail).check("new@example.org") is None


def test_sent_mail_lookups_are_cached(gmail):
    gmail.sent_to["ada@engines.example"] = ["Tue, 3 Mar 2026 10:00:00 -0800"]
    checker = SentMailChecker(gmail)

    checker.check("ada@engines.example")
    checker.check("ada@engines.example")

    assert len(gmail.searches) == 1


def test_a_search_failure_is_recorded_rather_than_raised(gmail):
    gmail.search_failures.add("ada@engines.example")
    checker = SentMailChecker(gmail)

    assert checker.check("ada@engines.example") is None
    assert len(checker.errors) == 1


def test_a_substring_match_is_not_treated_as_a_recipient(gmail):
    # Gmail can match a body mention, so the recipient headers are re-checked.
    gmail.sent_to["someone@else.example"] = ["Tue, 3 Mar 2026 10:00:00 -0800"]
    checker = SentMailChecker(gmail)

    assert checker.check("ada@engines.example") is None


def test_sent_mail_can_be_turned_off(gmail):
    gmail.sent_to["ada@engines.example"] = ["Tue, 3 Mar 2026 10:00:00 -0800"]
    checker = SentMailChecker(gmail, enabled=False)

    assert checker.check("ada@engines.example") is None
    assert gmail.searches == []


def test_guard_prefers_the_suppression_list_and_skips_the_api(gmail):
    gmail.sent_to["ada@engines.example"] = ["Tue, 3 Mar 2026 10:00:00 -0800"]
    guard = ContactGuard(
        suppression={"ada@engines.example": "asked not to be contacted"},
        sent_checker=SentMailChecker(gmail),
    )

    found = guard.check("ada@engines.example")

    assert found.source == SOURCE_SUPPRESSION
    assert found.detail == "asked not to be contacted"
    # A suppressed address should cost no API call.
    assert gmail.searches == []


def test_guard_checks_local_history_before_the_api(gmail):
    gmail.sent_to["ada@engines.example"] = ["Tue, 3 Mar 2026 10:00:00 -0800"]
    index = HistoryIndex()
    index.record("ada@engines.example", "2026-01-01T00:00:00+00:00")
    guard = ContactGuard(history=index, sent_checker=SentMailChecker(gmail))

    assert guard.check("ada@engines.example").source == SOURCE_PRIOR_BATCH
    assert gmail.searches == []


def test_guard_allows_an_address_with_no_prior_contact(gmail):
    guard = ContactGuard(sent_checker=SentMailChecker(gmail))

    assert guard.check("brand.new@example.org") is None


def test_guard_reports_partial_coverage_when_a_lookup_fails(gmail):
    gmail.search_failures.add("ada@engines.example")
    guard = ContactGuard(sent_checker=SentMailChecker(gmail))

    guard.check("ada@engines.example")

    assert guard.checked_sent_mail
    assert len(guard.sent_errors) == 1
