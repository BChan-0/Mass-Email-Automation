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
