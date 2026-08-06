"""Reading draft, scheduled, and sent state from Gmail."""

from __future__ import annotations

import pytest

from app.mailbox import (
    STATUS_PRECEDENCE,
    read_scheduled,
    scheduled_by_address,
    to_iso_date,
)


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


def test_sent_outranks_scheduled_which_outranks_draft():
    order = list(STATUS_PRECEDENCE)

    assert order.index("sent") < order.index("scheduled") < order.index("draft")
