"""Flask app: upload a CSV, edit templates, preview, create drafts, delete drafts.

Binds to loopback only. There is no login, so anyone who can reach the port can
use the connected Gmail account.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from flask import Flask, jsonify, render_template, request, session
from werkzeug.utils import secure_filename

from .config import (
    HISTORY_SLOW_THRESHOLD,
    MAX_ATTACHMENT_BYTES,
    MAX_CONTACTS,
    MAX_CSV_BYTES,
    PREVIEW_LIMIT,
    Paths,
    paths_from_env,
)
from .contacts import ParseResult, is_valid_email, parse_csv
from .drafts import TemplateSet, create_drafts, delete_drafts, render_all
from .gmail_client import (
    AuthError,
    GmailDraftService,
    GmailError,
    load_credentials,
    run_local_authorization,
)
from .history import (
    ContactGuard,
    SentMailChecker,
    build_history_index,
    fetch_live_draft_ids,
    load_suppression_list,
    normalize_address,
)
from .markup import looks_like_markdown, render_markdown
from .message import Attachment
from .store import BatchStore, load_settings, save_settings
from .templating import KNOWN_FIELDS, find_placeholders

# Contact fields the table lets you edit. Other CSV columns stay as imported.
EDITABLE_FIELDS = ("email", "first_name", "last_name", "company", "title")


def _decode_csv(raw: bytes) -> str:
    """Decode CSV bytes, trying the encodings Apollo and Excel produce."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _templates_from(payload: dict) -> TemplateSet:
    """Build a TemplateSet from a request body."""
    return TemplateSet(
        subject_template=str(payload.get("subject_template", "")),
        body_template=str(payload.get("body_template", "")),
        signoff_template=str(payload.get("signoff_template", "")),
        sender_name=str(payload.get("sender_name", "")),
        cc=str(payload.get("cc", "")),
        bcc=str(payload.get("bcc", "")),
        send_as_html=bool(payload.get("send_as_html", False)),
        use_markdown=bool(payload.get("use_markdown", False)),
    )


def create_app(paths: Paths | None = None) -> Flask:
    """Build the Flask app.

    :param paths: filesystem layout to use, for tests; defaults to the env layout
    :returns: the configured app
    """
    resolved = paths or paths_from_env()
    resolved.ensure()

    app = Flask(__name__, static_folder="static", template_folder="templates")
    # Session data is only a CSV cache key, so a per process key is enough; it does
    # mean uploads do not survive a restart.
    app.secret_key = secrets.token_hex(32)
    app.config["MAX_CONTENT_LENGTH"] = max(MAX_CSV_BYTES, MAX_ATTACHMENT_BYTES) + (1024 * 1024)
    app.config["PATHS"] = resolved

    store = BatchStore(resolved.batches)
    # Parsed CSVs and staged attachments, keyed by the id handed to the browser.
    csv_cache: dict[str, ParseResult] = {}
    csv_names: dict[str, str] = {}
    attachment_cache: dict[str, list[Attachment]] = {}

    def build_guard(service, *, skip_previously_emailed: bool = True) -> ContactGuard:
        """Assemble the prior contact check for one run.

        The suppression list always applies. Earlier drafts count only while they are
        still in the mailbox, which is confirmed against Gmail's own draft list.
        Searching sent mail is the part the user can turn off, since it costs one API
        call per contact.
        """
        checker = SentMailChecker(service, enabled=bool(skip_previously_emailed))
        live_ids, _reason = fetch_live_draft_ids(service)
        return ContactGuard(
            history=build_history_index(store.list_batches(), live_draft_ids=live_ids),
            suppression=load_suppression_list(resolved.suppression_file),
            sent_checker=checker,
        )

    def service_or_error() -> tuple[GmailDraftService | None, tuple]:
        """Return an authorized service, or a JSON error response to send back."""
        credentials = load_credentials(resolved.token_file)
        if credentials is None:
            return None, (
                jsonify({"ok": False, "error": "Not connected to Gmail. Click Connect Gmail first."}),
                401,
            )
        return GmailDraftService(credentials), ()

    @app.get("/")
    def index():
        """Serve the single page UI."""
        return render_template(
            "index.html",
            settings=load_settings(resolved.settings_file),
            known_fields=KNOWN_FIELDS,
            preview_limit=PREVIEW_LIMIT,
            max_contacts=MAX_CONTACTS,
        )

    @app.get("/api/status")
    def status():
        """Report whether Gmail is connected and which mailbox is in use."""
        client_secret_present = resolved.client_secret_file.exists()
        credentials = load_credentials(resolved.token_file)
        if credentials is None:
            return jsonify(
                {
                    "connected": False,
                    "client_secret_present": client_secret_present,
                    "email": "",
                }
            )
        try:
            email = GmailDraftService(credentials).profile_email()
        except GmailError as error:
            return jsonify(
                {
                    "connected": False,
                    "client_secret_present": client_secret_present,
                    "email": "",
                    "error": str(error),
                }
            )
        return jsonify({"connected": True, "client_secret_present": client_secret_present, "email": email})

    @app.post("/api/connect")
    def connect():
        """Run the OAuth flow. Opens a browser on this machine."""
        try:
            run_local_authorization(resolved.client_secret_file, resolved.token_file)
        except AuthError as error:
            return jsonify({"ok": False, "error": str(error)}), 400
        except Exception as error:
            return jsonify({"ok": False, "error": f"Authorization failed: {error}"}), 400

        try:
            email = GmailDraftService(load_credentials(resolved.token_file)).profile_email()
        except GmailError as error:
            return jsonify({"ok": False, "error": str(error)}), 400
        return jsonify({"ok": True, "email": email})

    @app.post("/api/disconnect")
    def disconnect():
        """Delete the stored token. Drafts already in Gmail are untouched."""
        resolved.token_file.unlink(missing_ok=True)
        return jsonify({"ok": True})

    @app.get("/api/settings")
    def get_settings():
        """Return saved templates and options."""
        return jsonify(load_settings(resolved.settings_file))

    @app.post("/api/settings")
    def post_settings():
        """Save templates and options for the next session."""
        payload = request.get_json(silent=True) or {}
        return jsonify(save_settings(resolved.settings_file, payload))

    @app.post("/api/upload-csv")
    def upload_csv():
        """Parse an uploaded CSV and report what was found."""
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return jsonify({"ok": False, "error": "No CSV file was attached."}), 400

        raw = uploaded.read(MAX_CSV_BYTES + 1)
        if len(raw) > MAX_CSV_BYTES:
            return jsonify({"ok": False, "error": f"CSV is larger than {MAX_CSV_BYTES // (1024 * 1024)} MB."}), 400

        parsed = parse_csv(_decode_csv(raw), MAX_CONTACTS)
        if not parsed.headers:
            return jsonify({"ok": False, "error": "That file has no header row."}), 400
        if "email" not in parsed.detected:
            return jsonify(
                {
                    "ok": False,
                    "error": "No email column found. Expected a header such as Email or Work Email.",
                    "headers": parsed.headers,
                }
            ), 400

        csv_id = secrets.token_urlsafe(12)
        csv_cache[csv_id] = parsed
        csv_names[csv_id] = secure_filename(uploaded.filename) or "contacts.csv"
        session["csv_id"] = csv_id

        return jsonify(
            {
                "ok": True,
                "csv_id": csv_id,
                "filename": csv_names[csv_id],
                "contact_count": len(parsed.contacts),
                "skipped": [
                    {"row": row.row_number, "email": row.email, "reason": row.reason} for row in parsed.skipped
                ],
                "headers": parsed.headers,
                "detected": parsed.detected,
                "available_fields": sorted({key for contact in parsed.contacts for key in contact.as_context()}),
            }
        )

    @app.get("/api/contacts")
    def get_contacts():
        """Return the parsed contacts so the UI can show an editable table."""
        csv_id = str(request.args.get("csv_id") or session.get("csv_id") or "")
        parsed = csv_cache.get(csv_id)
        if parsed is None:
            return jsonify({"ok": False, "error": "Upload a CSV first."}), 400

        return jsonify(
            {
                "ok": True,
                "csv_id": csv_id,
                "editable_fields": list(EDITABLE_FIELDS),
                "contacts": [
                    {
                        "index": index,
                        "row_number": contact.row_number,
                        "email": contact.email,
                        "first_name": contact.first_name,
                        "last_name": contact.last_name,
                        "company": contact.company,
                        "title": contact.title,
                    }
                    for index, contact in enumerate(parsed.contacts)
                ],
            }
        )

    @app.post("/api/contacts")
    def post_contacts():
        """Apply edits to the parsed contacts held for this upload.

        Edits live only in this process, alongside the parsed CSV. The uploaded file
        is never rewritten, so the original export stays as it was.
        """
        payload = request.get_json(silent=True) or {}
        csv_id = str(payload.get("csv_id") or session.get("csv_id") or "")
        parsed = csv_cache.get(csv_id)
        if parsed is None:
            return jsonify({"ok": False, "error": "Upload a CSV first."}), 400

        edits = payload.get("edits")
        removals = payload.get("remove")
        # Either half is optional, so removing rows without editing any is allowed.
        if not isinstance(edits, list) and not isinstance(removals, list):
            return jsonify({"ok": False, "error": "No edits or removals given."}), 400
        if not isinstance(edits, list):
            edits = []

        applied = 0
        rejected = []
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            try:
                index = int(edit.get("index", -1))
            except (TypeError, ValueError):
                continue
            if not 0 <= index < len(parsed.contacts):
                continue

            contact = parsed.contacts[index]
            for field_name in EDITABLE_FIELDS:
                if field_name not in edit:
                    continue
                value = str(edit[field_name] or "").strip()
                if field_name == "email":
                    if not is_valid_email(value):
                        rejected.append({"index": index, "email": value, "reason": "not a usable address"})
                        continue
                    contact.email = value
                else:
                    setattr(contact, field_name, value)
                # Keep the raw column view in step so templates referencing a CSV
                # header see the edited value too.
                contact.extra[field_name] = getattr(contact, field_name)
            applied += 1

        removed = 0
        if isinstance(removals, list):
            drop = {int(item) for item in removals if str(item).lstrip("-").isdigit()}
            if drop:
                kept = [contact for index, contact in enumerate(parsed.contacts) if index not in drop]
                removed = len(parsed.contacts) - len(kept)
                parsed.contacts[:] = kept

        return jsonify(
            {
                "ok": True,
                "applied": applied,
                "removed": removed,
                "rejected": rejected,
                "contact_count": len(parsed.contacts),
            }
        )

    @app.post("/api/upload-attachment")
    def upload_attachment():
        """Stage a file to attach to every draft in the next batch."""
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return jsonify({"ok": False, "error": "No file was attached."}), 400

        content = uploaded.read(MAX_ATTACHMENT_BYTES + 1)
        if len(content) > MAX_ATTACHMENT_BYTES:
            return jsonify(
                {"ok": False, "error": f"Attachment is larger than {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB."}
            ), 400

        filename = secure_filename(uploaded.filename) or "attachment"
        attachment_id = session.get("attachment_id") or secrets.token_urlsafe(12)
        session["attachment_id"] = attachment_id
        staged = attachment_cache.setdefault(attachment_id, [])
        staged = [item for item in staged if item.filename != filename]
        staged.append(
            Attachment(
                filename=filename,
                content=content,
                mime_type=uploaded.mimetype or "application/octet-stream",
            )
        )
        attachment_cache[attachment_id] = staged

        return jsonify(
            {
                "ok": True,
                "attachments": [
                    {"filename": item.filename, "size": len(item.content), "mime_type": item.mime_type}
                    for item in staged
                ],
            }
        )

    @app.post("/api/remove-attachment")
    def remove_attachment():
        """Unstage one attachment by filename."""
        payload = request.get_json(silent=True) or {}
        filename = str(payload.get("filename", ""))
        attachment_id = session.get("attachment_id", "")
        staged = [item for item in attachment_cache.get(attachment_id, []) if item.filename != filename]
        attachment_cache[attachment_id] = staged
        return jsonify(
            {
                "ok": True,
                "attachments": [
                    {"filename": item.filename, "size": len(item.content), "mime_type": item.mime_type}
                    for item in staged
                ],
            }
        )

    @app.post("/api/preview")
    def preview():
        """Render the first few contacts so templates can be checked before sending."""
        payload = request.get_json(silent=True) or {}
        csv_id = str(payload.get("csv_id") or session.get("csv_id") or "")
        parsed = csv_cache.get(csv_id)
        if parsed is None:
            return jsonify({"ok": False, "error": "Upload a CSV first."}), 400

        templates = _templates_from(payload)
        limit = max(1, min(int(payload.get("limit", PREVIEW_LIMIT) or PREVIEW_LIMIT), 25))
        rendered = render_all(parsed.contacts[:limit], templates)
        incomplete = sum(1 for item in render_all(parsed.contacts, templates) if not item.ok)

        return jsonify(
            {
                "ok": True,
                "total": len(parsed.contacts),
                "incomplete": incomplete,
                "placeholders": sorted(
                    set(find_placeholders(templates.subject_template))
                    | set(find_placeholders(templates.body_template))
                    | set(find_placeholders(templates.signoff_template))
                ),
                "use_markdown": templates.use_markdown,
                "markdown_unused": (
                    not templates.use_markdown
                    and looks_like_markdown(templates.body_template + templates.signoff_template)
                ),
                "drafts": [
                    {
                        "to": item.contact.email,
                        "subject": item.subject,
                        "body": item.body,
                        "html": render_markdown(item.body) if templates.use_markdown else "",
                        "missing": item.missing,
                    }
                    for item in rendered
                ],
            }
        )

    @app.post("/api/create-drafts")
    def post_create_drafts():
        """Create one Gmail draft per contact. Nothing is sent."""
        payload = request.get_json(silent=True) or {}
        csv_id = str(payload.get("csv_id") or session.get("csv_id") or "")
        parsed = csv_cache.get(csv_id)
        if parsed is None:
            return jsonify({"ok": False, "error": "Upload a CSV first."}), 400
        if not parsed.contacts:
            return jsonify({"ok": False, "error": "That CSV has no usable contacts."}), 400

        templates = _templates_from(payload)
        if not templates.subject_template.strip():
            return jsonify({"ok": False, "error": "Subject template is empty."}), 400
        if not templates.body_template.strip():
            return jsonify({"ok": False, "error": "Message template is empty."}), 400

        service, error_response = service_or_error()
        if service is None:
            return error_response

        attachments = attachment_cache.get(session.get("attachment_id", ""), [])
        outcome = create_drafts(
            service=service,
            store=store,
            contacts=parsed.contacts,
            templates=templates,
            attachments=attachments,
            source_name=csv_names.get(csv_id, "contacts.csv"),
            skip_incomplete=bool(payload.get("skip_incomplete", True)),
            guard=build_guard(service, skip_previously_emailed=payload.get("skip_previously_emailed", True)),
        )

        return jsonify(
            {
                "ok": True,
                "batch_id": outcome.batch.batch_id,
                "created": outcome.created,
                "failed": outcome.failed,
                "skipped": outcome.skipped,
                "blocked": outcome.blocked,
                "stopped_early": outcome.stopped_early,
                "failures": outcome.batch.failures,
                "skipped_rows": outcome.batch.skipped,
                "history_report": outcome.history_report,
            }
        )

    @app.post("/api/check-history")
    def check_history():
        """Report which contacts would be held back, without creating anything.

        Run this before Create drafts to see the prior contact report up front.
        """
        payload = request.get_json(silent=True) or {}
        csv_id = str(payload.get("csv_id") or session.get("csv_id") or "")
        parsed = csv_cache.get(csv_id)
        if parsed is None:
            return jsonify({"ok": False, "error": "Upload a CSV first."}), 400

        # A connection is needed either way: listing drafts confirms which earlier
        # drafts are still live, and that needs only the compose scope.
        check_sent = bool(payload.get("skip_previously_emailed", True))
        service, error_response = service_or_error()
        if service is None:
            return error_response

        guard = build_guard(service, skip_previously_emailed=check_sent)
        blocked = []
        for contact in parsed.contacts:
            prior = guard.check(contact.email)
            if prior is not None:
                blocked.append(prior.to_dict())

        by_source: dict[str, int] = {}
        for entry in blocked:
            source = str(entry["source"])
            by_source[source] = by_source.get(source, 0) + 1

        return jsonify(
            {
                "ok": True,
                "total": len(parsed.contacts),
                "blocked_count": len(blocked),
                "would_draft": len(parsed.contacts) - len(blocked),
                "blocked": blocked,
                "by_source": by_source,
                "checked_sent_mail": guard.checked_sent_mail,
                "sent_check_errors": guard.sent_errors,
                "slow_warning": len(parsed.contacts) > HISTORY_SLOW_THRESHOLD and check_sent,
            }
        )

    @app.get("/api/suppression")
    def get_suppression():
        """Return the do not contact list."""
        entries = load_suppression_list(resolved.suppression_file)
        return jsonify(
            {
                "ok": True,
                "path": str(resolved.suppression_file),
                "count": len(entries),
                "entries": [{"email": email, "note": note} for email, note in sorted(entries.items())],
            }
        )

    @app.post("/api/suppression")
    def post_suppression():
        """Add addresses to the do not contact list."""
        payload = request.get_json(silent=True) or {}
        raw = payload.get("emails")
        candidates = raw if isinstance(raw, list) else str(payload.get("email", "")).split()
        note = str(payload.get("note", "")).strip()

        additions = [normalize_address(str(item)) for item in candidates]
        additions = [address for address in additions if address]
        if not additions:
            return jsonify({"ok": False, "error": "No addresses given."}), 400

        existing = load_suppression_list(resolved.suppression_file)
        new_entries = [address for address in additions if address not in existing]

        if new_entries:
            resolved.suppression_file.parent.mkdir(parents=True, exist_ok=True)
            with resolved.suppression_file.open("a", encoding="utf-8") as stream:
                for address in new_entries:
                    stream.write(f"{address}, {note}\n" if note else f"{address}\n")

        return jsonify({"ok": True, "added": len(new_entries), "count": len(existing) + len(new_entries)})

    @app.get("/api/batches")
    def list_batches():
        """List every batch this app has created."""
        return jsonify({"ok": True, "batches": [batch.to_dict() for batch in store.list_batches()]})

    @app.get("/api/batches/<batch_id>")
    def get_batch(batch_id: str):
        """Return one batch record."""
        batch = store.load(batch_id)
        if batch is None:
            return jsonify({"ok": False, "error": "No such batch."}), 404
        return jsonify({"ok": True, "batch": batch.to_dict()})

    @app.post("/api/batches/<batch_id>/delete-drafts")
    def post_delete_drafts(batch_id: str):
        """Delete drafts from a batch. Omit draft_ids to delete all of them."""
        batch = store.load(batch_id)
        if batch is None:
            return jsonify({"ok": False, "error": "No such batch."}), 404

        service, error_response = service_or_error()
        if service is None:
            return error_response

        payload = request.get_json(silent=True) or {}
        requested = payload.get("draft_ids")
        draft_ids = [str(item) for item in requested] if isinstance(requested, list) else None

        outcome = delete_drafts(service=service, store=store, batch=batch, draft_ids=draft_ids)
        return jsonify(
            {
                "ok": True,
                "deleted": outcome.deleted,
                "failures": outcome.failures,
                "batch": store.load(batch_id).to_dict(),
            }
        )

    @app.post("/api/batches/<batch_id>/forget")
    def forget_batch(batch_id: str):
        """Remove a batch record. Drafts still in Gmail are left alone."""
        if not store.delete_record(batch_id):
            return jsonify({"ok": False, "error": "No such batch."}), 404
        return jsonify({"ok": True})

    @app.errorhandler(413)
    def too_large(_error):
        """Turn Flask's upload limit into a JSON error the UI can display."""
        return jsonify({"ok": False, "error": "That upload is too large."}), 413

    return app


def main() -> None:
    """Entry point for ``python -m app.web``."""
    import argparse

    parser = argparse.ArgumentParser(description="Run the Gmail draft builder UI.")
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5000, help="port to listen on (default 5000)")
    parser.add_argument("--debug", action="store_true", help="enable Flask debug mode")
    parser.add_argument("--root", default=None, help="directory for data and credentials")
    arguments = parser.parse_args()

    paths = Paths(root=Path(arguments.root).expanduser().resolve()) if arguments.root else paths_from_env()
    create_app(paths).run(host=arguments.host, port=arguments.port, debug=arguments.debug)


if __name__ == "__main__":
    main()
