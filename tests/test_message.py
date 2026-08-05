"""MIME assembly and encoding."""

from __future__ import annotations

import base64
import email

from app.message import Attachment, body_to_html, build_message, encode_message, sanitize_header


def test_plain_message_headers_and_body():
    message = build_message(to="ada@engines.example", subject="Hello", body="Line one\nLine two")

    assert message["To"] == "ada@engines.example"
    assert message["Subject"] == "Hello"
    assert "Line one" in message.get_content()


def test_from_header_is_omitted_when_no_sender_is_given():
    message = build_message(to="ada@engines.example", subject="Hello", body="Body")

    assert message["From"] is None


def test_html_alternative_is_added_on_request():
    message = build_message(to="a@b.example", subject="S", body="Para one\n\nPara two", as_html=True)
    subtypes = {part.get_content_subtype() for part in message.walk() if not part.is_multipart()}

    assert subtypes == {"plain", "html"}


def test_attachments_keep_their_filename_and_type():
    attachment = Attachment(filename="deck.pdf", content=b"%PDF fake", mime_type="application/pdf")
    message = build_message(to="a@b.example", subject="S", body="B", attachments=[attachment])

    parts = [part for part in message.walk() if part.get_filename()]
    assert len(parts) == 1
    assert parts[0].get_filename() == "deck.pdf"
    assert parts[0].get_content_type() == "application/pdf"


def test_attachment_from_path_guesses_the_type(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")

    attachment = Attachment.from_path(path)

    assert attachment.filename == "notes.txt"
    assert attachment.content == b"hello"
    assert attachment.mime_type == "text/plain"


def test_encode_message_round_trips():
    message = build_message(to="a@b.example", subject="Subject line", body="Body text")

    decoded = base64.urlsafe_b64decode(encode_message(message).encode("ascii"))
    parsed = email.message_from_bytes(decoded)

    assert parsed["Subject"] == "Subject line"
    assert "Body text" in decoded.decode("utf-8")


def test_a_newline_in_a_field_cannot_inject_a_header():
    # A CSV cell holding "Ada\nBcc: attacker@evil.example" must not add a header.
    message = build_message(to="a@b.example", subject="Hi Ada\nBcc: attacker@evil.example", body="Body")

    assert message["Bcc"] is None
    assert message["Subject"] == "Hi Ada Bcc: attacker@evil.example"


def test_a_newline_in_a_field_does_not_raise():
    # email.message rejects CR and LF in headers, which would abort a whole batch.
    message = build_message(to="a@b.example\r\n", subject="S\r\nX: y", body="Body")

    assert message["To"] == "a@b.example"


def test_sanitize_header_collapses_whitespace():
    assert sanitize_header("Ada\nLovelace") == "Ada Lovelace"
    assert sanitize_header("  spaced \t out  ") == "spaced out"
    assert sanitize_header("") == ""


def test_body_keeps_its_newlines():
    message = build_message(to="a@b.example", subject="S", body="Line one\nLine two")

    assert "Line one\nLine two" in message.get_content()


def test_markdown_bodies_produce_formatted_html():
    message = build_message(
        to="a@b.example",
        subject="S",
        body="Hi **Ada**,\n\n- one\n- two\n",
        as_html=True,
        as_markdown=True,
    )
    parts = {part.get_content_subtype(): part for part in message.walk() if not part.is_multipart()}

    assert "<strong>Ada</strong>" in parts["html"].get_content()
    assert parts["html"].get_content().count("<li>") == 2
    # The plain text part keeps the Markdown source, which reads fine as text.
    assert "**Ada**" in parts["plain"].get_content()


def test_markdown_html_is_sanitized_in_the_message():
    message = build_message(
        to="a@b.example",
        subject="S",
        body="Hi <script>alert(1)</script> **Ada**",
        as_html=True,
        as_markdown=True,
    )
    html = next(
        part.get_content()
        for part in message.walk()
        if not part.is_multipart() and part.get_content_subtype() == "html"
    )

    assert "<script" not in html.lower()
    assert "<strong>Ada</strong>" in html


def test_markdown_off_escapes_syntax_rather_than_rendering_it():
    message = build_message(to="a@b.example", subject="S", body="Hi **Ada**", as_html=True)
    html = next(
        part.get_content()
        for part in message.walk()
        if not part.is_multipart() and part.get_content_subtype() == "html"
    )

    assert "<strong>" not in html
    assert "**Ada**" in html


def test_body_to_html_renders_markdown_on_request():
    assert "<strong>x</strong>" in body_to_html("**x**", as_markdown=True)
    assert "<strong>" not in body_to_html("**x**")


def test_body_to_html_escapes_and_keeps_breaks():
    html = body_to_html("Ada & Grace\nsecond line\n\nnew paragraph")

    assert "Ada &amp; Grace<br>second line" in html
    assert html.count("<p>") == 2


def test_body_to_html_on_empty_input():
    assert body_to_html("") == "<p></p>"


def test_unicode_subject_and_body_survive_encoding():
    message = build_message(to="a@b.example", subject="Café ☕", body="Naïve résumé")

    decoded = base64.urlsafe_b64decode(encode_message(message).encode("ascii"))
    parsed = email.message_from_bytes(decoded)

    assert parsed["Subject"] is not None
    assert "Naïve résumé" in parsed.get_payload(decode=True).decode("utf-8")
