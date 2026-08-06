"""Reading draft, scheduled, and sent state from Gmail."""

from __future__ import annotations

import pytest

from app.gmail_client import is_bounce_sender
from app.mailbox import (
    SHEET_STATUS,
    STATUS_PRECEDENCE,
    read_reply_senders,
    read_scheduled,
    scheduled_by_address,
    to_iso_date,
    to_sheet_date,
)
from app.tracker import STATUS_CHOICES


def test_scheduled_messages_are_read_with_their_send_time(gmail):
    gmail.scheduled = [
        {
            "id": "m1",
            "to": "ada@engines.example",
            "subject": "[Harvard Product Lab x Engines] Fall 26",
            "date": "Wed, 5 Aug 2026 17:00:00 -0700",
        }
    ]

    messages, error = read_scheduled(gmail)

    assert error == ""
    assert len(messages) == 1
    assert messages[0].to == "ada@engines.example"
    assert messages[0].subject.endswith("Fall 26")
    assert messages[0].send_date == "2026-08-05"


def test_no_scheduled_messages_is_not_an_error(gmail):
    messages, error = read_scheduled(gmail)

    assert messages == []
    assert error == ""


def test_a_failure_is_reported_rather_than_raised(gmail):
    gmail.scheduled_fails = True

    messages, error = read_scheduled(gmail)

    assert messages == []
    assert "could not" in error


def test_without_a_connection_the_reason_is_given():
    messages, error = read_scheduled(None)

    assert messages == []
    assert error == "no Gmail connection"


def test_scheduled_messages_are_indexed_by_every_recipient(gmail):
    gmail.scheduled = [
        {
            "id": "m1",
            "to": "ada@engines.example",
            "cc": "grace@compilers.example",
            "subject": "S",
            "date": "Wed, 5 Aug 2026 17:00:00 -0700",
        }
    ]

    index = scheduled_by_address(read_scheduled(gmail)[0])

    assert set(index) == {"ada@engines.example", "grace@compilers.example"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Wed, 5 Aug 2026 17:00:00 -0700", "2026-08-05"),
        ("2026-08-05T12:00:00+00:00", "2026-08-05"),
        ("", ""),
        ("not a date", ""),
    ],
)
def test_dates_are_reduced_to_a_plain_date(raw, expected):
    assert to_iso_date(raw) == expected


def test_a_reply_outranks_sent_which_outranks_scheduled_then_draft():
    order = list(STATUS_PRECEDENCE)

    assert order.index("replied") < order.index("sent") < order.index("scheduled") < order.index("draft")


def test_replies_and_bounces_are_read_separately(gmail):
    gmail.replied = {"ada@engines.example"}
    gmail.bounced = {"gone@engines.example"}

    senders, bounced, error = read_reply_senders(gmail)

    assert senders == {"ada@engines.example"}
    assert bounced == {"gone@engines.example"}
    assert error == ""


def test_a_reply_lookup_failure_is_reported(gmail):
    gmail.replies_fail = True

    senders, bounced, error = read_reply_senders(gmail)

    assert senders == set()
    assert bounced == set()
    assert "could not search for replies" in error


def test_reply_lookup_without_a_connection():
    senders, bounced, error = read_reply_senders(None)

    assert senders == set()
    assert bounced == set()
    assert error == "no Gmail connection"


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("mailer-daemon@googlemail.com", True),
        ("postmaster@example.com", True),
        ("ada@engines.example", False),
    ],
)
def test_bounce_senders_are_recognized(address, expected):
    # A bounce lands in the thread it failed on, so it looks like a reply until named.
    assert is_bounce_sender(address) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Wed, 5 Aug 2026 17:00:00 -0700", "8/5/2026"),
        ("2026-12-09T00:00:00+00:00", "12/9/2026"),
        ("", ""),
    ],
)
def test_sheet_dates_drop_leading_zeros(raw, expected):
    # The sheet writes 8/5/2026, so an ISO date would not match the column.
    assert to_sheet_date(raw) == expected


def test_every_sheet_status_is_a_real_dropdown_entry():
    # Sheets only draws its coloured chip when the text matches the dropdown exactly,
    # so a wording the sheet does not define would paste as a plain cell.
    for status, wording in SHEET_STATUS.items():
        assert wording in STATUS_CHOICES, status


def test_the_sheet_status_wording_matches_the_dropdown():
    assert SHEET_STATUS["sent"] == "Reached Out"
    assert SHEET_STATUS["replied"] == "Replied"
    assert SHEET_STATUS["scheduled"] == "Scheduled Email"
    assert SHEET_STATUS["bounced"] == "email failed :("
    # Nothing reached the person, so the sheet's own wording for that is used.
    assert SHEET_STATUS["draft"] == "Not Reached Out"
    assert SHEET_STATUS["deleted"] == "Not Reached Out"
