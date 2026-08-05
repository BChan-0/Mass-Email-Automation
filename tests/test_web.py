"""HTTP endpoints, with Gmail replaced by the stub."""

from __future__ import annotations

import io

import pytest

from app import web
from app.store import BatchStore

from .conftest import FakeGmail


@pytest.fixture
def client(paths):
    app = web.create_app(paths)
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture
def connected(monkeypatch, paths):
    """Make the app behave as though Gmail is authorized, backed by the stub."""
    gmail = FakeGmail()
    monkeypatch.setattr(web, "load_credentials", lambda _token_file: object())
    monkeypatch.setattr(web, "GmailDraftService", lambda _credentials: gmail)
    return gmail


def upload(client, csv_text, filename="apollo.csv"):
    return client.post(
        "/api/upload-csv",
        data={"file": (io.BytesIO(csv_text.encode("utf-8")), filename)},
        content_type="multipart/form-data",
    )


def test_index_renders(client):
    response = client.get("/")

    assert response.status_code == 200
    assert b"Gmail Draft Builder" in response.data


def test_status_reports_disconnected_without_a_token(client):
    payload = client.get("/api/status").get_json()

    assert payload["connected"] is False
    assert payload["client_secret_present"] is False


def test_status_reports_the_mailbox_when_connected(client, connected):
    payload = client.get("/api/status").get_json()

    assert payload["connected"] is True
    assert payload["email"] == "tester@example.com"


def test_upload_reports_contacts_and_detected_columns(client, apollo_csv):
    payload = upload(client, apollo_csv).get_json()

    assert payload["ok"] is True
    assert payload["contact_count"] == 3
    assert payload["detected"]["email"] == "Email"
    assert len(payload["skipped"]) == 3
    assert "seniority" in payload["available_fields"]


def test_upload_without_a_file_is_rejected(client):
    response = client.post("/api/upload-csv", data={}, content_type="multipart/form-data")

    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_upload_without_an_email_column_is_rejected(client):
    response = upload(client, "Name,Company\nAda,Engines\n")

    assert response.status_code == 400
    assert "No email column" in response.get_json()["error"]


def test_preview_renders_the_first_contacts(client, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    payload = client.post(
        "/api/preview",
        json={
            "csv_id": csv_id,
            "subject_template": "Hi {{company}}",
            "body_template": "Hello {{first_name}}",
            "signoff_template": "Best,\n{{sender_name}}",
            "sender_name": "Bonnie",
            "limit": 2,
        },
    ).get_json()

    assert payload["total"] == 3
    assert len(payload["drafts"]) == 2
    assert payload["drafts"][0]["subject"] == "Hi Analytical Engines"
    assert "Best,\nBonnie" in payload["drafts"][0]["body"]
    assert payload["placeholders"] == ["company", "first_name", "sender_name"]


def test_preview_counts_contacts_with_missing_fields(client, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    payload = client.post(
        "/api/preview",
        json={"csv_id": csv_id, "subject_template": "{{title}}", "body_template": "Hi"},
    ).get_json()

    assert payload["incomplete"] == 1


def test_preview_before_upload_is_rejected(client):
    response = client.post("/api/preview", json={"subject_template": "s", "body_template": "b"})

    assert response.status_code == 400


def test_create_drafts_requires_a_connection(client, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    response = client.post(
        "/api/create-drafts",
        json={"csv_id": csv_id, "subject_template": "Hi", "body_template": "Body"},
    )

    assert response.status_code == 401
    assert "Not connected" in response.get_json()["error"]


def test_create_drafts_writes_a_batch(client, connected, apollo_csv, paths):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    payload = client.post(
        "/api/create-drafts",
        json={
            "csv_id": csv_id,
            "subject_template": "Hi {{company}}",
            "body_template": "Hello {{first_name}}",
            "signoff_template": "Best,\nBonnie",
        },
    ).get_json()

    assert payload["created"] == 3
    assert payload["failed"] == 0
    assert len(connected.drafts) == 3
    assert BatchStore(paths.batches).load(payload["batch_id"]).live_count == 3


def test_create_drafts_rejects_an_empty_subject(client, connected, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    response = client.post(
        "/api/create-drafts", json={"csv_id": csv_id, "subject_template": "  ", "body_template": "Body"}
    )

    assert response.status_code == 400
    assert "Subject template is empty" in response.get_json()["error"]


def test_contacts_are_returned_for_the_table(client, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    payload = client.get(f"/api/contacts?csv_id={csv_id}").get_json()

    assert len(payload["contacts"]) == 3
    first = payload["contacts"][0]
    assert first["email"] == "ada@engines.example"
    assert first["company"] == "Analytical Engines"
    assert first["row_number"] == 2
    assert "email" in payload["editable_fields"]


def test_contacts_needs_an_upload_first(client):
    assert client.get("/api/contacts").status_code == 400


def test_editing_a_contact_changes_what_is_drafted(client, connected, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    edited = client.post(
        "/api/contacts",
        json={
            "csv_id": csv_id,
            "edits": [{"index": 0, "first_name": "Augusta", "title": "Countess"}],
        },
    ).get_json()

    assert edited["applied"] == 1
    preview = client.post(
        "/api/preview",
        json={"csv_id": csv_id, "subject_template": "Hi {{first_name}}", "body_template": "{{title}}"},
    ).get_json()
    assert preview["drafts"][0]["subject"] == "Hi Augusta"
    assert "Countess" in preview["drafts"][0]["body"]


def test_a_corrected_address_is_used(client, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    client.post(
        "/api/contacts",
        json={"csv_id": csv_id, "edits": [{"index": 0, "email": "ada.fixed@engines.example"}]},
    )

    contacts = client.get(f"/api/contacts?csv_id={csv_id}").get_json()["contacts"]
    assert contacts[0]["email"] == "ada.fixed@engines.example"


def test_an_invalid_edited_address_is_rejected_and_the_old_one_kept(client, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    payload = client.post(
        "/api/contacts",
        json={"csv_id": csv_id, "edits": [{"index": 0, "email": "not-an-address"}]},
    ).get_json()

    assert len(payload["rejected"]) == 1
    contacts = client.get(f"/api/contacts?csv_id={csv_id}").get_json()["contacts"]
    assert contacts[0]["email"] == "ada@engines.example"


def test_removing_a_row_drops_it_from_the_batch(client, connected, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    removed = client.post("/api/contacts", json={"csv_id": csv_id, "remove": [1]}).get_json()

    assert removed["removed"] == 1
    assert removed["contact_count"] == 2
    created = client.post(
        "/api/create-drafts",
        json={"csv_id": csv_id, "subject_template": "Hi", "body_template": "Body"},
    ).get_json()
    assert created["created"] == 2
    assert "grace@compilers.example" not in [draft["to"] for draft in connected.drafts.values()]


def test_an_out_of_range_edit_is_ignored(client, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    payload = client.post(
        "/api/contacts",
        json={"csv_id": csv_id, "edits": [{"index": 99, "first_name": "Nobody"}]},
    ).get_json()

    assert payload["applied"] == 0
    assert payload["contact_count"] == 3


def test_contacts_endpoint_rejects_a_bad_payload(client, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    assert client.post("/api/contacts", json={"csv_id": csv_id}).status_code == 400


def test_preview_returns_rendered_html_in_markdown_mode(client, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    payload = client.post(
        "/api/preview",
        json={
            "csv_id": csv_id,
            "subject_template": "Hi",
            "body_template": "Hello **{{first_name}}**",
            "use_markdown": True,
        },
    ).get_json()

    assert payload["use_markdown"] is True
    assert "<strong>Ada</strong>" in payload["drafts"][0]["html"]
    assert "**Ada**" in payload["drafts"][0]["body"]


def test_preview_warns_when_markdown_is_off_but_present(client, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    payload = client.post(
        "/api/preview",
        json={"csv_id": csv_id, "subject_template": "Hi", "body_template": "Hello **there**"},
    ).get_json()

    assert payload["markdown_unused"] is True
    assert payload["drafts"][0]["html"] == ""


def test_markdown_reaches_the_created_draft(client, connected, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    client.post(
        "/api/create-drafts",
        json={
            "csv_id": csv_id,
            "subject_template": "Hi",
            "body_template": "Hello **{{first_name}}**",
            "use_markdown": True,
        },
    )

    raw = next(iter(connected.drafts.values()))["raw"].decode("utf-8", "replace")
    assert "<strong>" in raw or "strong" in raw


def test_attachment_is_staged_and_can_be_removed(client):
    staged = client.post(
        "/api/upload-attachment",
        data={"file": (io.BytesIO(b"%PDF fake"), "deck.pdf")},
        content_type="multipart/form-data",
    ).get_json()

    assert staged["attachments"][0]["filename"] == "deck.pdf"

    removed = client.post("/api/remove-attachment", json={"filename": "deck.pdf"}).get_json()

    assert removed["attachments"] == []


def test_staged_attachment_reaches_created_drafts(client, connected, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]
    client.post(
        "/api/upload-attachment",
        data={"file": (io.BytesIO(b"%PDF fake"), "deck.pdf")},
        content_type="multipart/form-data",
    )

    payload = client.post(
        "/api/create-drafts",
        json={"csv_id": csv_id, "subject_template": "Hi", "body_template": "Body"},
    ).get_json()

    assert payload["created"] == 3
    batch = client.get(f"/api/batches/{payload['batch_id']}").get_json()["batch"]
    assert batch["attachment_names"] == ["deck.pdf"]


def test_uploading_the_same_filename_twice_does_not_duplicate_it(client):
    for _ in range(2):
        payload = client.post(
            "/api/upload-attachment",
            data={"file": (io.BytesIO(b"data"), "deck.pdf")},
            content_type="multipart/form-data",
        ).get_json()

    assert len(payload["attachments"]) == 1


def test_delete_all_drafts_in_a_batch(client, connected, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]
    batch_id = client.post(
        "/api/create-drafts",
        json={"csv_id": csv_id, "subject_template": "Hi", "body_template": "Body"},
    ).get_json()["batch_id"]

    payload = client.post(f"/api/batches/{batch_id}/delete-drafts", json={}).get_json()

    assert payload["deleted"] == 3
    assert payload["batch"]["live_count"] == 0
    assert connected.drafts == {}


def test_delete_selected_drafts_only(client, connected, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]
    batch_id = client.post(
        "/api/create-drafts",
        json={"csv_id": csv_id, "subject_template": "Hi", "body_template": "Body"},
    ).get_json()["batch_id"]
    drafts = client.get(f"/api/batches/{batch_id}").get_json()["batch"]["drafts"]

    payload = client.post(
        f"/api/batches/{batch_id}/delete-drafts", json={"draft_ids": [drafts[0]["draft_id"]]}
    ).get_json()

    assert payload["deleted"] == 1
    assert payload["batch"]["live_count"] == 2
    assert len(connected.drafts) == 2


def test_delete_requires_a_connection(client, apollo_csv, paths):
    store = BatchStore(paths.batches)
    batch = store.new_batch(source_name="a.csv", subject_template="s", attachment_names=[])
    store.save(batch)

    response = client.post(f"/api/batches/{batch.batch_id}/delete-drafts", json={})

    assert response.status_code == 401


def test_unknown_batch_returns_404(client, connected):
    assert client.get("/api/batches/missing").status_code == 404
    assert client.post("/api/batches/missing/delete-drafts", json={}).status_code == 404
    assert client.post("/api/batches/missing/forget", json={}).status_code == 404


def test_forget_removes_the_record_but_not_the_drafts(client, connected, apollo_csv, paths):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]
    batch_id = client.post(
        "/api/create-drafts",
        json={"csv_id": csv_id, "subject_template": "Hi", "body_template": "Body"},
    ).get_json()["batch_id"]

    assert client.post(f"/api/batches/{batch_id}/forget", json={}).get_json()["ok"] is True
    assert BatchStore(paths.batches).load(batch_id) is None
    assert len(connected.drafts) == 3


def test_create_drafts_blocks_a_previously_emailed_contact(client, connected, apollo_csv):
    connected.sent_to["grace@compilers.example"] = ["Tue, 3 Mar 2026 10:00:00 -0800"]
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    payload = client.post(
        "/api/create-drafts",
        json={"csv_id": csv_id, "subject_template": "Hi", "body_template": "Body"},
    ).get_json()

    assert payload["created"] == 2
    assert payload["blocked"] == 1
    report = payload["history_report"]
    assert report["blocked"][0]["email"] == "grace@compilers.example"
    assert report["blocked"][0]["first_contact"] == "2026-03-03"
    assert report["coverage_complete"] is True


def test_the_sent_mail_check_can_be_turned_off(client, connected, apollo_csv):
    connected.sent_to["grace@compilers.example"] = ["Tue, 3 Mar 2026 10:00:00 -0800"]
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    payload = client.post(
        "/api/create-drafts",
        json={
            "csv_id": csv_id,
            "subject_template": "Hi",
            "body_template": "Body",
            "skip_previously_emailed": False,
        },
    ).get_json()

    assert payload["created"] == 3
    assert payload["blocked"] == 0
    assert payload["history_report"]["checked_sent_mail"] is False


def test_check_history_reports_without_creating_anything(client, connected, apollo_csv):
    connected.sent_to["grace@compilers.example"] = ["Tue, 3 Mar 2026 10:00:00 -0800"]
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    payload = client.post("/api/check-history", json={"csv_id": csv_id}).get_json()

    assert payload["total"] == 3
    assert payload["blocked_count"] == 1
    assert payload["would_draft"] == 2
    assert connected.drafts == {}


def test_check_history_needs_a_csv(client, connected):
    assert client.post("/api/check-history", json={}).status_code == 400


def test_check_history_needs_a_connection_when_searching_sent_mail(client, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    response = client.post("/api/check-history", json={"csv_id": csv_id})

    assert response.status_code == 401


def test_suppression_list_round_trip(client, connected, apollo_csv):
    added = client.post(
        "/api/suppression",
        json={"emails": ["Grace@Compilers.Example"], "note": "asked to be removed"},
    ).get_json()

    assert added["added"] == 1
    listed = client.get("/api/suppression").get_json()
    assert listed["entries"][0]["email"] == "grace@compilers.example"
    assert listed["entries"][0]["note"] == "asked to be removed"


def test_a_suppressed_address_is_not_drafted(client, connected, apollo_csv):
    client.post("/api/suppression", json={"emails": ["grace@compilers.example"]})
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]

    payload = client.post(
        "/api/create-drafts",
        json={"csv_id": csv_id, "subject_template": "Hi", "body_template": "Body"},
    ).get_json()

    assert payload["created"] == 2
    assert payload["history_report"]["by_source"] == {"suppression_list": 1}


def test_adding_the_same_address_twice_does_not_duplicate_it(client, connected):
    client.post("/api/suppression", json={"emails": ["ada@engines.example"]})
    second = client.post("/api/suppression", json={"emails": ["Ada@Engines.Example"]}).get_json()

    assert second["added"] == 0
    assert client.get("/api/suppression").get_json()["count"] == 1


def test_suppression_rejects_an_empty_request(client):
    assert client.post("/api/suppression", json={}).status_code == 400


def test_running_the_same_csv_twice_blocks_the_second_run(client, connected, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]
    body = {"csv_id": csv_id, "subject_template": "Hi", "body_template": "Body"}

    first = client.post("/api/create-drafts", json=body).get_json()
    second = client.post("/api/create-drafts", json=body).get_json()

    assert first["created"] == 3
    assert second["created"] == 0
    assert second["blocked"] == 3
    assert second["history_report"]["by_source"] == {"prior_batch": 3}


def test_deleting_a_batch_frees_the_contacts_to_be_drafted_again(client, connected, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]
    body = {"csv_id": csv_id, "subject_template": "Hi", "body_template": "Body"}

    first = client.post("/api/create-drafts", json=body).get_json()
    assert first["created"] == 3

    # A second run is blocked while those drafts are still waiting.
    blocked = client.post("/api/create-drafts", json=body).get_json()
    assert blocked["created"] == 0
    assert blocked["blocked"] == 3

    # Deleting them means nothing was sent and nothing is pending.
    client.post(f"/api/batches/{first['batch_id']}/delete-drafts", json={})

    again = client.post("/api/create-drafts", json=body).get_json()
    assert again["created"] == 3
    assert again["blocked"] == 0


def test_deleting_one_draft_frees_only_that_contact(client, connected, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]
    body = {"csv_id": csv_id, "subject_template": "Hi", "body_template": "Body"}
    first = client.post("/api/create-drafts", json=body).get_json()
    drafts = client.get(f"/api/batches/{first['batch_id']}").get_json()["batch"]["drafts"]
    freed = drafts[0]

    client.post(
        f"/api/batches/{first['batch_id']}/delete-drafts",
        json={"draft_ids": [freed["draft_id"]]},
    )

    again = client.post("/api/create-drafts", json=body).get_json()

    assert again["created"] == 1
    assert again["blocked"] == 2
    assert freed["to"] in [draft["to"] for draft in connected.drafts.values()]


def test_a_draft_deleted_directly_in_gmail_stops_blocking(client, connected, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]
    body = {"csv_id": csv_id, "subject_template": "Hi", "body_template": "Body"}
    client.post("/api/create-drafts", json=body)

    # Delete outside this app, so the local record still calls it live.
    gone = next(iter(connected.drafts))
    freed_address = connected.drafts[gone]["to"]
    del connected.drafts[gone]

    again = client.post("/api/create-drafts", json=body).get_json()

    assert again["created"] == 1
    assert freed_address in [draft["to"] for draft in connected.drafts.values()]


def test_forgetting_a_record_also_forgets_that_a_draft_is_waiting(client, connected, apollo_csv):
    # The record links a draft id to an address, so removing it loses the block even
    # though the draft is still in Gmail. The UI warns about this before doing it.
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]
    body = {"csv_id": csv_id, "subject_template": "Hi", "body_template": "Body"}
    first = client.post("/api/create-drafts", json=body).get_json()

    client.post(f"/api/batches/{first['batch_id']}/forget", json={})
    again = client.post("/api/create-drafts", json=body).get_json()

    assert again["created"] == 3
    assert len(connected.drafts) == 6


def test_a_sent_draft_still_blocks_through_sent_mail(client, connected, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]
    body = {"csv_id": csv_id, "subject_template": "Hi", "body_template": "Body"}
    client.post("/api/create-drafts", json=body)

    # Sending a draft removes it from the draft list but puts it in sent mail.
    sent_id = next(iter(connected.drafts))
    sent_address = connected.drafts[sent_id]["to"]
    del connected.drafts[sent_id]
    connected.sent_to[sent_address] = ["Tue, 3 Mar 2026 10:00:00 -0800"]

    again = client.post("/api/create-drafts", json=body).get_json()
    still_blocked = [entry["email"] for entry in again["history_report"]["blocked"]]

    assert sent_address in still_blocked
    assert again["created"] == 0


def test_batches_endpoint_lists_saved_runs(client, connected, apollo_csv):
    csv_id = upload(client, apollo_csv).get_json()["csv_id"]
    client.post("/api/create-drafts", json={"csv_id": csv_id, "subject_template": "Hi", "body_template": "B"})

    payload = client.get("/api/batches").get_json()

    assert len(payload["batches"]) == 1
    assert payload["batches"][0]["source_name"] == "apollo.csv"


def test_settings_round_trip(client):
    saved = client.post("/api/settings", json={"sender_name": "Bonnie", "cc": "team@example.com"}).get_json()

    assert saved["sender_name"] == "Bonnie"
    assert client.get("/api/settings").get_json()["cc"] == "team@example.com"


def test_disconnect_removes_the_token(client, paths):
    paths.token_file.write_text("{}", encoding="utf-8")

    assert client.post("/api/disconnect", json={}).get_json()["ok"] is True
    assert not paths.token_file.exists()


def test_connect_reports_a_missing_client_secret(client):
    response = client.post("/api/connect", json={})

    assert response.status_code == 400
    assert "client_secret.json" in response.get_json()["error"]


def test_latin1_csv_is_decoded(client):
    csv_bytes = "Email,Company\nrené@example.com,Café\n".encode("cp1252")
    response = client.post(
        "/api/upload-csv",
        data={"file": (io.BytesIO(csv_bytes), "latin.csv")},
        content_type="multipart/form-data",
    )

    assert response.get_json()["contact_count"] == 1
