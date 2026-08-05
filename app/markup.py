"""Markdown rendering for message bodies.

Bodies are written in Markdown and sent as a two part message: the Markdown source
as the plain text part, and rendered HTML as the alternative. A mail client that
cannot show HTML still gets readable text.

Rendered HTML is sanitized because a body carries values from the uploaded CSV,
which is third party data. Markdown passes raw HTML through by default, so a
crafted cell could otherwise inject script into the preview page or the message.
"""

from __future__ import annotations

import bleach
import markdown

# nl2br makes a single newline a line break, which is what someone writing an email
# expects. sane_lists stops a stray number in a sentence from starting a list.
MARKDOWN_EXTENSIONS = ["nl2br", "sane_lists"]

# Tags an email body needs. Deliberately narrow: no img, no style, no table, since
# mail clients handle those inconsistently and they widen the attack surface.
ALLOWED_TAGS = frozenset(
    {
        "p",
        "br",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "ul",
        "ol",
        "li",
        "a",
        "blockquote",
        "code",
        "pre",
        "h1",
        "h2",
        "h3",
        "hr",
    }
)

ALLOWED_ATTRIBUTES = {"a": ["href", "title"]}
ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


def render_markdown(text: str) -> str:
    """Convert Markdown to sanitized HTML.

    :param text: Markdown source, usually a rendered message body
    :returns: HTML safe to place in a page or an email part
    """
    if not (text or "").strip():
        return ""
    html = markdown.markdown(text, extensions=MARKDOWN_EXTENSIONS)
    return bleach.clean(
        html,
        tags=set(ALLOWED_TAGS),
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    ).strip()


def looks_like_markdown(text: str) -> bool:
    """Guess whether text uses any Markdown formatting.

    Used to warn when Markdown mode is off but the body appears to contain syntax
    that would otherwise be sent as literal asterisks.

    :param text: message body to inspect
    :returns: True when common Markdown syntax is present
    """
    import re

    patterns = (
        r"\*\*[^*\n]+\*\*",  # bold
        r"(?<![\w*])\*[^*\n]+\*(?![\w*])",  # italic
        r"^\s*[-*+]\s+\S",  # bullet list
        r"^\s*\d+\.\s+\S",  # numbered list
        r"\[[^\]\n]+\]\([^)\n]+\)",  # link
        r"^\s*#{1,3}\s+\S",  # heading
        r"^\s*>\s+\S",  # quote
    )
    return any(re.search(pattern, text or "", re.MULTILINE) for pattern in patterns)
