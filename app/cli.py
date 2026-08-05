"""Command line access to the same draft operations the web UI uses.

Useful for scripting and for checking a template render without a browser.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import MAX_CONTACTS, Paths, paths_from_env
from .contacts import parse_csv
from .drafts import TemplateSet, create_drafts, delete_drafts, render_all
from .gmail_client import (
    AuthError,
    GmailDraftService,
    GmailError,
    load_credentials,
    run_local_authorization,
)
from .message import Attachment
from .store import BatchStore, load_settings


def _read_csv(path: Path):
    """Parse a CSV file, trying the encodings Apollo and Excel produce."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return parse_csv(path.read_text(encoding=encoding), MAX_CONTACTS)
        except UnicodeDecodeError:
            continue
    return parse_csv(path.read_text(encoding="utf-8", errors="replace"), MAX_CONTACTS)


def _templates(arguments, paths: Paths) -> TemplateSet:
    """Build a TemplateSet from arguments, falling back to saved settings."""
    saved = load_settings(paths.settings_file)
    body = arguments.body
    if arguments.body_file:
        body = Path(arguments.body_file).read_text(encoding="utf-8")
    return TemplateSet(
        subject_template=arguments.subject or str(saved["subject_template"]),
        body_template=body or str(saved["body_template"]),
        signoff_template=arguments.signoff if arguments.signoff is not None else str(saved["signoff_template"]),
        sender_name=arguments.sender_name or str(saved["sender_name"]),
        cc=arguments.cc or "",
        bcc=arguments.bcc or "",
        send_as_html=arguments.html,
    )


def _service(paths: Paths) -> GmailDraftService:
    """Return an authorized service or exit with instructions."""
    credentials = load_credentials(paths.token_file)
    if credentials is None:
        raise SystemExit("Not connected to Gmail. Run: python -m app.cli auth")
    return GmailDraftService(credentials)


def command_auth(arguments, paths: Paths) -> int:
    """Run the OAuth flow and save a token."""
    try:
        run_local_authorization(paths.client_secret_file, paths.token_file, port=arguments.port)
    except AuthError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Connected as {_service(paths).profile_email()}")
    return 0


def command_preview(arguments, paths: Paths) -> int:
    """Render the first few contacts and print them."""
    parsed = _read_csv(Path(arguments.csv))
    if not parsed.contacts:
        print("No usable contacts in that CSV.", file=sys.stderr)
        return 1

    for rendered in render_all(parsed.contacts[: arguments.limit], _templates(arguments, paths)):
        print("=" * 72)
        print(f"To:      {rendered.contact.email}")
        print(f"Subject: {rendered.subject}")
        print()
        print(rendered.body)
        if rendered.missing:
            print(f"[missing fields: {', '.join(rendered.missing)}]")
    print("=" * 72)
    print(f"{len(parsed.contacts)} contacts, {len(parsed.skipped)} rows skipped during parse")
    return 0


def command_create(arguments, paths: Paths) -> int:
    """Create one draft per contact."""
    parsed = _read_csv(Path(arguments.csv))
    if not parsed.contacts:
        print("No usable contacts in that CSV.", file=sys.stderr)
        return 1

    attachments = [Attachment.from_path(Path(item)) for item in arguments.attach or []]
    store = BatchStore(paths.batches)
    outcome = create_drafts(
        service=_service(paths),
        store=store,
        contacts=parsed.contacts,
        templates=_templates(arguments, paths),
        attachments=attachments,
        source_name=Path(arguments.csv).name,
        skip_incomplete=not arguments.allow_incomplete,
    )
    print(
        f"batch {outcome.batch.batch_id}: {outcome.created} created, {outcome.skipped} skipped, {outcome.failed} failed"
    )
    for failure in outcome.batch.failures[:10]:
        print(f"  failed {failure['email']}: {failure['error']}", file=sys.stderr)
    return 0 if outcome.created and not outcome.stopped_early else 1


def command_list(_arguments, paths: Paths) -> int:
    """List recorded batches."""
    batches = BatchStore(paths.batches).list_batches()
    if not batches:
        print("No batches recorded.")
        return 0
    for batch in batches:
        print(
            f"{batch.batch_id}  {batch.created_at}  {batch.source_name}  "
            f"{batch.live_count} live, {batch.deleted_count} deleted"
        )
    return 0


def command_delete(arguments, paths: Paths) -> int:
    """Delete drafts from a recorded batch."""
    store = BatchStore(paths.batches)
    batch = store.load(arguments.batch_id)
    if batch is None:
        print(f"No batch {arguments.batch_id}.", file=sys.stderr)
        return 1

    targets = arguments.draft_id or None
    count = len(targets) if targets else batch.live_count
    if not count:
        print("Nothing to delete.")
        return 0
    if not arguments.yes:
        answer = input(f"Delete {count} draft(s) from batch {batch.batch_id}? [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            print("Cancelled.")
            return 1

    outcome = delete_drafts(service=_service(paths), store=store, batch=batch, draft_ids=targets)
    print(f"{outcome.deleted} deleted, {len(outcome.failures)} failed")
    for failure in outcome.failures[:10]:
        print(f"  failed {failure['draft_id']}: {failure['error']}", file=sys.stderr)
    return 0 if not outcome.failures else 1


def build_parser() -> argparse.ArgumentParser:
    """Define the command line interface."""
    parser = argparse.ArgumentParser(prog="app.cli", description="Create and delete Gmail drafts from a CSV.")
    parser.add_argument("--root", default=None, help="directory for data and credentials")
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth = subparsers.add_parser("auth", help="authorize Gmail access")
    auth.add_argument("--port", type=int, default=0, help="loopback port for the OAuth redirect")
    auth.set_defaults(handler=command_auth)

    def add_template_arguments(target: argparse.ArgumentParser) -> None:
        target.add_argument("csv", help="path to the contact CSV")
        target.add_argument("--subject", default=None, help="subject template")
        target.add_argument("--body", default=None, help="message template")
        target.add_argument("--body-file", default=None, help="read the message template from a file")
        target.add_argument("--signoff", default=None, help="sign off template")
        target.add_argument("--sender-name", default=None, help="value for the sender_name placeholder")
        target.add_argument("--cc", default=None, help="comma separated Cc addresses")
        target.add_argument("--bcc", default=None, help="comma separated Bcc addresses")
        target.add_argument("--html", action="store_true", help="include an HTML version of the message")

    preview = subparsers.add_parser("preview", help="render drafts without touching Gmail")
    add_template_arguments(preview)
    preview.add_argument("--limit", type=int, default=3, help="how many contacts to render")
    preview.set_defaults(handler=command_preview)

    create = subparsers.add_parser("create", help="create Gmail drafts")
    add_template_arguments(create)
    create.add_argument("--attach", action="append", help="file to attach, repeatable")
    create.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="draft contacts with unresolved placeholders instead of skipping them",
    )
    create.set_defaults(handler=command_create)

    listing = subparsers.add_parser("batches", help="list recorded batches")
    listing.set_defaults(handler=command_list)

    delete = subparsers.add_parser("delete", help="delete drafts from a batch")
    delete.add_argument("batch_id", help="batch id from the batches command")
    delete.add_argument("--draft-id", action="append", help="specific draft id, repeatable")
    delete.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    delete.set_defaults(handler=command_delete)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch."""
    arguments = build_parser().parse_args(argv)
    paths = Paths(root=Path(arguments.root).expanduser().resolve()) if arguments.root else paths_from_env()
    paths.ensure()
    try:
        return arguments.handler(arguments, paths)
    except GmailError as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
