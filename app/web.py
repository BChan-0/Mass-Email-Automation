"""Flask app: upload a CSV, edit templates, preview, create drafts, delete drafts.

Defaults to binding loopback only. There is no login, so anyone who can reach the
port can use the connected Gmail account.
"""

from __future__ import annotations

import argparse
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
from .library import CsvLibrary, apply_saved_edits
from .mailbox import (
    SHEET_STATUS,
    STATUS_LABELS,
    read_reply_senders,
    read_scheduled,
    scheduled_by_address,
)
from .markup import looks_like_markdown, render_markdown
from .message import Attachment
from .store import BatchStore, load_settings, save_settings
from .templating import KNOWN_FIELDS, find_placeholders
from .tracker import (
    ASSIGNEE_CHOICES,
    COLUMNS,
    RE_EMAILED_CHOICES,
    STATUS_CHOICES,
    TrackerFields,
    build_rows,
    merge_states,
    to_tsv,
)

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
    # Session data is only the CSV and attachment cache keys, so a per process key is
    # enough; it does mean uploads do not survive a restart.
    app.secret_key = secrets.token_hex(32)
    app.config["MAX_CONTENT_LENGTH"] = max(MAX_CSV_BYTES, MAX_ATTACHMENT_BYTES) + (1024 * 1024)
    app.config["PATHS"] = resolved

    store = BatchStore(resolved.batches)
    library = CsvLibrary(resolved.library)
    tracker = TrackerFields(resolved.tracker_file)
    # Parsed CSVs and staged attachments, keyed by the id handed to the browser.
    csv_cache: dict[str, ParseResult] = {}
    csv_names: dict[str, str] = {}
    attachment_cache: dict[str, list[Attachment]] = {}
    # Which saved list an upload came from, and which of its rows were edited.
    csv_lists: dict[str, str] = {}
    csv_edited: dict[str, dict[int, list[str]]] = {}

    def build_guard(service, *, skip_previously_emailed: bool = True) -> ContactGuard:
        """Assemble the prior contact check for one run.

        The suppression list always applies. Earlier drafts count only while they are
        still in the mailbox, confirmed against Gmail's own draft list; if that listing
        fails the local record is trusted instead. Searching sent mail is the part the
        user can turn off, since it costs one API call per contact.
        """
        checker = SentMailChecker(service, enabled=bool(skip_previously_emailed))
        live_ids, _reason = fetch_live_draft_ids(service)
        # Scheduled messages are held outside the drafts list, so they need their own
        # lookup or a pending send would be duplicated.
        scheduled, _scheduled_error = read_scheduled(service)
        return ContactGuard(
            history=build_history_index(store.list_batches(), live_draft_ids=live_ids),
            suppression=load_suppression_list(resolved.suppression_file),
            sent_checker=checker,
            scheduled=scheduled_by_address(scheduled),
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

        # Remember the upload so the list can be reused without the file. Failing to
        # save is not worth losing the upload over, so it is swallowed and
        # saved_list_id comes back empty.
        saved_list_id = ""
        try:
            entry = library.save_upload(name=csv_names[csv_id], raw=raw, row_count=len(parsed.contacts))
            saved_list_id = entry.list_id
            csv_lists[csv_id] = entry.list_id
            # A returning file brings its earlier edits back with it.
            if entry.edits or entry.removed:
                kept, edited = apply_saved_edits(parsed.contacts, entry)
                parsed.contacts[:] = kept
                csv_edited[csv_id] = edited
        except OSError:
            saved_list_id = ""

        return jsonify(
            {
                "ok": True,
                "csv_id": csv_id,
                "saved_list_id": saved_list_id,
                "edited_rows": {str(index): names for index, names in csv_edited.get(csv_id, {}).items()},
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

        Edits apply to the parsed CSV in this process and are recorded against the
        saved list, so they come back next time. The uploaded file is never rewritten,
        so the original export stays as it was.
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
        # Only values that differ from what is loaded are recorded, so the browser
        # resending every cell on each save does not mark the whole row as edited.
        changed: dict[int, dict[str, str]] = {}
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
                if value == getattr(contact, field_name):
                    continue
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
                changed.setdefault(index, {})[field_name] = getattr(contact, field_name)
            applied += 1

        removed = 0
        if isinstance(removals, list):
            drop = {int(item) for item in removals if str(item).lstrip("-").isdigit()}
            if drop:
                kept = [contact for index, contact in enumerate(parsed.contacts) if index not in drop]
                removed = len(parsed.contacts) - len(kept)
                parsed.contacts[:] = kept

        # Persist the edits against the saved list so they come back next time.
        list_id = csv_lists.get(csv_id, "")
        if list_id:
            library.record_edits(
                list_id,
                edits=changed,
                removed=[int(item) for item in (removals or []) if str(item).isdigit()],
            )

        return jsonify(
            {
                "ok": True,
                "applied": applied,
                "removed": removed,
                "rejected": rejected,
                "contact_count": len(parsed.contacts),
                "saved_to_library": bool(list_id),
            }
        )

    @app.get("/api/library")
    def get_library():
        """List remembered uploads, most recently used first."""
        return jsonify({"ok": True, "lists": [item.to_dict() for item in library.list_all()]})

    @app.post("/api/library/<list_id>/load")
    def load_saved_list(list_id: str):
        """Reload a remembered upload with its edits replayed."""
        entry = library.load(list_id)
        raw = library.read_csv(list_id)
        if entry is None or raw is None:
            return jsonify({"ok": False, "error": "No such saved list."}), 404

        parsed = parse_csv(_decode_csv(raw), MAX_CONTACTS)
        kept, edited = apply_saved_edits(parsed.contacts, entry)
        parsed.contacts[:] = kept

        csv_id = secrets.token_urlsafe(12)
        csv_cache[csv_id] = parsed
        csv_names[csv_id] = entry.name
        csv_lists[csv_id] = list_id
        csv_edited[csv_id] = edited
        session["csv_id"] = csv_id
        library.touch(list_id)

        return jsonify(
            {
                "ok": True,
                "csv_id": csv_id,
                "filename": entry.name,
                "contact_count": len(parsed.contacts),
                "edited_rows": {str(index): names for index, names in edited.items()},
                "removed_count": len(entry.removed),
                "headers": parsed.headers,
                "detected": parsed.detected,
                "available_fields": sorted({key for c in parsed.contacts for key in c.as_context()}),
            }
        )

    @app.post("/api/library/<list_id>/forget-edits")
    def forget_saved_edits(list_id: str):
        """Drop the saved edits, leaving the original upload."""
        if library.clear_edits(list_id) is None:
            return jsonify({"ok": False, "error": "No such saved list."}), 404
        return jsonify({"ok": True})

    @app.post("/api/library/<list_id>/delete")
    def delete_saved_list(list_id: str):
        """Forget a saved upload entirely."""
        if not library.delete(list_id):
            return jsonify({"ok": False, "error": "No such saved list."}), 404
        return jsonify({"ok": True})

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

    @app.get("/api/message-status")
    def message_status():
        """Report the state of every address this app has drafted to.

        Scheduled messages are included whether or not this app created them, since
        Gmail holds them outside the drafts list.
        """
        service, error_response = service_or_error()
        if service is None:
            return error_response

        check_sent = request.args.get("check_sent", "1") != "0"
        scheduled, scheduled_error = read_scheduled(service)
        live_ids, live_error = fetch_live_draft_ids(service)
        replied, bounced, reply_error = read_reply_senders(service) if check_sent else (set(), set(), "")
        batches = store.list_batches()
        # Sent mail has to be consulted, or a message that has left the drafts list
        # because it was sent would be reported as deleted.
        checker = SentMailChecker(service, enabled=check_sent)
        states = merge_states(
            scheduled=scheduled_by_address(scheduled),
            live_draft_ids=live_ids,
            batches=batches,
            sent_lookup=checker.check if checker.enabled else None,
            replied=replied,
            bounced=bounced,
        )

        rows = [
            {
                "email": address,
                "status": state.status,
                "label": STATUS_LABELS.get(state.status, state.status),
                "sheet_status": SHEET_STATUS.get(state.status, ""),
                "subject": state.subject,
                "when": state.when,
                "re_emailed": state.re_emailed,
                "replied": address in replied,
                "bounced": address in bounced,
            }
            for address, state in sorted(states.items())
        ]
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1

        return jsonify(
            {
                "ok": True,
                "rows": rows,
                "counts": counts,
                "total": len(rows),
                "scheduled_total": len(scheduled),
                "replied_total": sum(1 for row in rows if row["replied"]),
                "bounced_total": sum(1 for row in rows if row["bounced"]),
                "checked_replies": check_sent,
                "errors": [item for item in (scheduled_error, live_error, reply_error) if item],
            }
        )

    @app.post("/api/tracker")
    def build_tracker():
        """Build spreadsheet rows for the current CSV, ready to paste."""
        payload = request.get_json(silent=True) or {}
        csv_id = str(payload.get("csv_id") or session.get("csv_id") or "")
        parsed = csv_cache.get(csv_id)
        if parsed is None:
            return jsonify({"ok": False, "error": "Upload a CSV first."}), 400

        service, error_response = service_or_error()
        if service is None:
            return error_response

        check_sent = bool(payload.get("check_sent", True))
        scheduled, scheduled_error = read_scheduled(service)
        live_ids, _live_error = fetch_live_draft_ids(service)
        replied, bounced, reply_error = read_reply_senders(service) if check_sent else (set(), set(), "")
        checker = SentMailChecker(service, enabled=check_sent)
        states = merge_states(
            scheduled=scheduled_by_address(scheduled),
            live_draft_ids=live_ids,
            batches=store.list_batches(),
            sent_lookup=checker.check if checker.enabled else None,
            replied=replied,
            bounced=bounced,
        )

        rows = build_rows(
            parsed.contacts,
            states=states,
            saved=tracker,
            default_assignee=str(payload.get("default_assignee", "")),
        )
        return jsonify(
            {
                "ok": True,
                "columns": list(COLUMNS),
                "rows": [row.to_dict() for row in rows],
                "cells": [row.as_cells() for row in rows],
                # No header by default, since rows are appended below what the sheet
                # already holds. The browser builds the same text as you edit cells.
                "tsv": to_tsv(rows, include_header=bool(payload.get("include_header", False))),
                "status_choices": list(STATUS_CHOICES),
                "re_emailed_choices": list(RE_EMAILED_CHOICES),
                "assignee_choices": list(ASSIGNEE_CHOICES),
                "errors": [item for item in (scheduled_error, reply_error) if item],
            }
        )

    @app.get("/api/tracker/saved")
    def list_tracker_fields():
        """Show every remembered value, so it is clear what is stored."""
        entries = tracker.all_entries()
        return jsonify(
            {
                "ok": True,
                "path": str(resolved.tracker_file),
                "count": len(entries),
                "entries": [{"email": email, **values} for email, values in sorted(entries.items())],
            }
        )

    @app.post("/api/tracker/forget")
    def forget_tracker_fields():
        """Clear remembered values, for named addresses or all of them."""
        payload = request.get_json(silent=True) or {}
        emails = payload.get("emails")
        if payload.get("all"):
            removed = tracker.forget_all()
        elif isinstance(emails, list) and emails:
            removed = tracker.forget([str(item) for item in emails])
        else:
            return jsonify({"ok": False, "error": "Give addresses to clear, or all."}), 400

        tracker.save()
        return jsonify({"ok": True, "removed": removed, "remaining": tracker.count()})

    @app.post("/api/tracker/save")
    def save_tracker_fields():
        """Remember the spreadsheet values typed for one or more contacts."""
        payload = request.get_json(silent=True) or {}
        entries = payload.get("entries")
        if not isinstance(entries, list) or not entries:
            return jsonify({"ok": False, "error": "No entries given."}), 400

        saved = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            email = str(entry.get("email", ""))
            if not email:
                continue
            tracker.update(email, entry)
            saved += 1
        tracker.save()
        return jsonify({"ok": True, "saved": saved, "remembered": tracker.count()})

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
