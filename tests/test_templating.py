"""Placeholder substitution."""

from __future__ import annotations

from app.templating import build_context, find_placeholders, normalize_key, render


def test_substitutes_known_fields():
    result = render("Hi {{first_name}} at {{company}}", {"first_name": "Ada", "company": "Engines"})

    assert result.text == "Hi Ada at Engines"
    assert result.ok


def test_uses_the_default_after_a_pipe():
    result = render("Hi {{first_name|there}}", {"first_name": ""})

    assert result.text == "Hi there"
    assert result.ok


def test_reports_missing_fields_and_leaves_syntax_in_place():
    result = render("Hi {{first_name}} at {{company}}", {"first_name": "Ada"})

    assert result.missing == ["company"]
    assert "{{company}}" in result.text


def test_whitespace_inside_braces_is_allowed():
    assert render("{{  company  }}", {"company": "Engines"}).text == "Engines"


def test_placeholder_names_are_case_insensitive():
    assert render("{{Company}}", {"company": "Engines"}).text == "Engines"


def test_values_are_stripped():
    assert render("{{company}}", {"company": "  Engines  "}).text == "Engines"


def test_find_placeholders_is_ordered_and_deduplicated():
    found = find_placeholders("{{company}} {{first_name}} {{company}}")

    assert found == ["company", "first_name"]


def test_normalize_key_handles_export_headers():
    assert normalize_key("First Name") == "first_name"
    assert normalize_key("  Company/Org  ") == "company_org"
    assert normalize_key("#") == ""


def test_build_context_adds_sender_fields():
    context = build_context({"Email": "a@b.example"}, sender_name="Bonnie", signoff="Best")

    assert context["email"] == "a@b.example"
    assert context["sender_name"] == "Bonnie"
    assert context["signoff"] == "Best"


def test_text_without_placeholders_is_unchanged():
    result = render("No fields here", {})

    assert result.text == "No fields here"
    assert result.ok
