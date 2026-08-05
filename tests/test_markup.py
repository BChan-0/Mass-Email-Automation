"""Markdown rendering and sanitizing."""

from __future__ import annotations

import pytest

from app.markup import looks_like_markdown, render_markdown


def test_bold_and_italic_become_html():
    html = render_markdown("Hi **Ada**, your *work* stood out.")

    assert "<strong>Ada</strong>" in html
    assert "<em>work</em>" in html


def test_lists_are_rendered():
    html = render_markdown("Points:\n\n- first\n- second\n")

    assert html.count("<li>") == 2
    assert "<ul>" in html


def test_links_are_kept():
    html = render_markdown("See [our site](https://example.com).")

    assert '<a href="https://example.com">our site</a>' in html


def test_a_single_newline_becomes_a_line_break():
    # Someone writing an email expects a newline to show as a newline.
    html = render_markdown("Best,\nBonnie")

    assert "<br" in html


def test_paragraphs_are_separate():
    html = render_markdown("First para.\n\nSecond para.")

    assert html.count("<p>") == 2


@pytest.mark.parametrize(
    "payload",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "<iframe src=https://evil.example></iframe>",
        "<style>body{display:none}</style>",
        '<div onclick="alert(1)">click</div>',
    ],
)
def test_dangerous_html_is_stripped(payload):
    # A body carries CSV values, which are third party data.
    html = render_markdown(f"Hi {payload} there")

    for tag in ("<script", "<img", "<iframe", "<style", "onerror", "onclick"):
        assert tag not in html.lower()


def test_a_javascript_url_is_neutralized():
    html = render_markdown("[click](javascript:alert(1))")

    assert "javascript:" not in html.lower()


def test_a_mailto_link_survives():
    html = render_markdown("[mail me](mailto:ada@engines.example)")

    assert "mailto:ada@engines.example" in html


def test_empty_input_renders_nothing():
    assert render_markdown("") == ""
    assert render_markdown("   ") == ""


def test_plain_text_still_renders_as_a_paragraph():
    assert render_markdown("Just a sentence.") == "<p>Just a sentence.</p>"


@pytest.mark.parametrize(
    "text",
    [
        "Hi **Ada**",
        "Some *emphasis* here",
        "- a bullet",
        "1. a number",
        "[link](https://example.com)",
        "# Heading",
        "> quoted",
    ],
)
def test_markdown_syntax_is_detected(text):
    assert looks_like_markdown(text)


@pytest.mark.parametrize(
    "text",
    [
        "Hi Ada, no formatting here.",
        "",
        "A sentence with 2 numbers and 3 things.",
        "5 * 4 is twenty",
        "email me at a@b.example",
    ],
)
def test_plain_text_is_not_flagged_as_markdown(text):
    assert not looks_like_markdown(text)
