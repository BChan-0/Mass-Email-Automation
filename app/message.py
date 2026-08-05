"""MIME assembly for draft creation."""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from .markup import render_markdown


@dataclass(frozen=True)
class Attachment:
    """A file to attach, already read into memory."""

    filename: str
    content: bytes
    mime_type: str = "application/octet-stream"

    @classmethod
    def from_path(cls, path: Path) -> Attachment:
        """Read a file from disk and guess its MIME type from the extension."""
        guessed, _ = mimetypes.guess_type(path.name)
        return cls(
            filename=path.name,
            content=path.read_bytes(),
            mime_type=guessed or "application/octet-stream",
        )


def sanitize_header(value: str) -> str:
    """Collapse newlines and tabs in a value destined for a mail header.

    Email headers cannot contain CR or LF, and a CSV cell holding either would
    otherwise abort message assembly. Collapsing also closes off header injection
    through a crafted contact field.

    :param value: raw rendered header text
    :returns: single line text safe to assign to a header
    """
    return " ".join((value or "").split())


def body_to_html(body: str, *, as_markdown: bool = False) -> str:
    """Convert a message body into HTML.

    :param body: rendered body text
    :param as_markdown: treat the body as Markdown rather than plain text
    :returns: HTML for the alternative part
    """
    if as_markdown:
        return render_markdown(body) or "<p></p>"

    from html import escape

    paragraphs = [block.strip() for block in (body or "").replace("\r\n", "\n").split("\n\n")]
    rendered = ["<p>" + escape(block).replace("\n", "<br>") + "</p>" for block in paragraphs if block]
    return "\n".join(rendered) or "<p></p>"


def build_message(
    *,
    to: str,
    subject: str,
    body: str,
    sender: str = "",
    cc: str = "",
    bcc: str = "",
    attachments: list[Attachment] | None = None,
    as_html: bool = False,
    as_markdown: bool = False,
) -> EmailMessage:
    """Assemble a single message.

    :param to: recipient address
    :param subject: rendered subject line
    :param body: rendered body, including the sign off
    :param sender: From address; left unset so Gmail fills in the authorized user
    :param cc: comma separated Cc addresses
    :param bcc: comma separated Bcc addresses
    :param attachments: files to attach to this message
    :param as_html: also send an HTML alternative built from the body
    :param as_markdown: read the body as Markdown when building that alternative
    :returns: the assembled message
    """
    message = EmailMessage()
    # Header values are sanitized because they can carry rendered CSV content.
    message["To"] = sanitize_header(to)
    message["Subject"] = sanitize_header(subject)
    if sender:
        message["From"] = sanitize_header(sender)
    if cc:
        message["Cc"] = sanitize_header(cc)
    if bcc:
        message["Bcc"] = sanitize_header(bcc)

    # The plain text part keeps the Markdown source, which stays readable as text.
    message.set_content(body or "")
    if as_html:
        message.add_alternative(body_to_html(body, as_markdown=as_markdown), subtype="html")

    for attachment in attachments or []:
        main_type, _, sub_type = attachment.mime_type.partition("/")
        message.add_attachment(
            attachment.content,
            maintype=main_type or "application",
            subtype=sub_type or "octet-stream",
            filename=attachment.filename,
        )

    return message


def encode_message(message: EmailMessage) -> str:
    """Encode a message as the base64url string the Gmail API expects."""
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
