"""CSV parsing and contact normalization."""

from __future__ import annotations

import pytest

from app.contacts import is_valid_email, parse_csv


def test_parses_apollo_export(apollo_csv):
    result = parse_csv(apollo_csv, max_contacts=100)

    assert [contact.email for contact in result.contacts] == [
        "ada@engines.example",
        "grace@compilers.example",
        "alan@bletchley.example",
    ]
    assert result.detected["email"] == "Email"
    assert result.detected["company"] == "Company"
    assert result.detected["title"] == "Title"


def test_reports_skipped_rows_with_reasons(apollo_csv):
    result = parse_csv(apollo_csv, max_contacts=100)
    reasons = [row.reason for row in result.skipped]

    assert len(result.skipped) == 3
    assert any("duplicate" in reason for reason in reasons)
    assert sum("unusable" in reason for reason in reasons) == 2


def test_keeps_every_column_for_templates(apollo_csv):
    result = parse_csv(apollo_csv, max_contacts=100)
    context = result.contacts[0].as_context()

    assert context["seniority"] == "vp"
    assert context["full_name"] == "Ada Lovelace"


def test_splits_a_single_name_column():
    csv_text = "Name,Email,Organization\nMary Ann Evans,mary@example.com,Middlemarch\n"
    result = parse_csv(csv_text, max_contacts=10)
    contact = result.contacts[0]

    assert contact.first_name == "Mary"
    assert contact.last_name == "Ann Evans"
    assert contact.company == "Middlemarch"


def test_matches_alternate_header_names():
    csv_text = "work_email,job_title,company_name,first_name\na@b.example,Founder,Acme,Ann\n"
    result = parse_csv(csv_text, max_contacts=10)

    assert result.contacts[0].title == "Founder"
    assert result.contacts[0].company == "Acme"


def test_a_decoy_name_column_is_not_read_as_the_full_name():
    # Apollo exports carry "Company Name for Emails" next to a real first name column.
    csv_text = (
        "First Name,Last Name,Title,Company,Company Name for Emails,Email\n"
        ",Hopper,Chief Scientist,Compiler Works,Compiler Works Inc,grace@compilers.example\n"
    )
    result = parse_csv(csv_text, max_contacts=10)
    contact = result.contacts[0]

    assert "full_name" not in result.detected
    assert contact.company == "Compiler Works"
    assert contact.first_name == ""
    assert contact.last_name == "Hopper"


def test_each_column_is_claimed_by_only_one_field():
    csv_text = "Email,Name,Company\na@b.example,Ada Lovelace,Engines\n"
    result = parse_csv(csv_text, max_contacts=10)

    assert len(set(result.detected.values())) == len(result.detected)


def test_no_email_column_yields_no_contacts():
    result = parse_csv("Name,Company\nAda,Engines\n", max_contacts=10)

    assert result.contacts == []
    assert "email" not in result.detected


def test_row_limit_is_enforced(apollo_csv):
    result = parse_csv(apollo_csv, max_contacts=2)

    assert len(result.contacts) == 2
    assert any("row limit" in row.reason for row in result.skipped)


def test_handles_excel_bom_and_crlf():
    csv_text = "﻿Email,Company\r\nada@engines.example,Engines\r\n"
    result = parse_csv(csv_text, max_contacts=10)

    assert result.contacts[0].email == "ada@engines.example"
    assert result.contacts[0].company == "Engines"


def test_empty_input_is_not_an_error():
    result = parse_csv("", max_contacts=10)

    assert result.contacts == []
    assert result.headers == []


def test_email_validation_rejects_placeholders():
    assert is_valid_email("ada@engines.example")
    assert is_valid_email("<ada@engines.example>")
    assert not is_valid_email("email_not_unlocked")
    assert not is_valid_email("ada@engines")
    assert not is_valid_email("")
    assert not is_valid_email("a@b.example, c@d.example")


@pytest.mark.parametrize(
    "sentinel",
    [
        "email_not_unlocked@domain.com",
        "EMAIL_NOT_UNLOCKED@Domain.com",
        "not_unlocked@domain.com",
        "email_not_found@domain.com",
    ],
)
def test_gated_apollo_addresses_are_rejected(sentinel):
    # Apollo writes a syntactically valid sentinel for contacts you have not
    # unlocked, so a pattern check alone would draft to a junk domain.
    assert not is_valid_email(sentinel)


def test_a_gated_row_is_skipped_in_the_real_export_format():
    csv_text = (
        "First Name,Last Name,Title,Company,Email,Email Status\n"
        "Ada,Lovelace,VP,Engines,ada@engines.example,Verified\n"
        "Locked,Contact,Growth Lead,Hidden Co,email_not_unlocked@domain.com,Unavailable\n"
    )
    result = parse_csv(csv_text, max_contacts=10)

    assert [contact.email for contact in result.contacts] == ["ada@engines.example"]
    assert len(result.skipped) == 1
    assert "unusable" in result.skipped[0].reason
