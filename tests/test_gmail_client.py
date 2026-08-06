"""Credential loading, including the weekly expiry that Testing status imposes."""

from __future__ import annotations

import json

from app import gmail_client
from app.gmail_client import load_credentials

TOKEN = {
    "token": "access-token",
    "refresh_token": "refresh-token",
    "client_id": "id.apps.googleusercontent.com",
    "client_secret": "GOCSPX-value",
    "token_uri": "https://oauth2.googleapis.com/token",
    "scopes": ["https://www.googleapis.com/auth/gmail.compose"],
}


def test_a_missing_token_file_is_not_an_error(paths):
    assert load_credentials(paths.token_file) is None


def test_a_corrupt_token_file_is_not_an_error(paths):
    paths.token_file.write_text("{not json", encoding="utf-8")

    assert load_credentials(paths.token_file) is None


def test_a_valid_token_is_returned_unchanged(paths, monkeypatch):
    paths.token_file.write_text(json.dumps(TOKEN), encoding="utf-8")
    monkeypatch.setattr(gmail_client.Credentials, "valid", property(lambda _self: True))

    assert load_credentials(paths.token_file) is not None
    assert paths.token_file.exists()


def test_an_unrefreshable_token_is_removed(paths, monkeypatch):
    # Google expires the refresh token seven days after consent while the app's
    # publishing status is Testing, so this is the routine weekly case.
    paths.token_file.write_text(json.dumps(TOKEN), encoding="utf-8")
    monkeypatch.setattr(gmail_client.Credentials, "valid", property(lambda _self: False))
    monkeypatch.setattr(gmail_client.Credentials, "expired", property(lambda _self: True))

    def refuse(_self, _request):
        raise gmail_client.GmailError("invalid_grant")

    monkeypatch.setattr(gmail_client.Credentials, "refresh", refuse)

    assert load_credentials(paths.token_file) is None
    assert not paths.token_file.exists()


def test_a_refreshed_token_is_saved_with_owner_only_permissions(paths, monkeypatch):
    paths.token_file.write_text(json.dumps(TOKEN), encoding="utf-8")
    monkeypatch.setattr(gmail_client.Credentials, "valid", property(lambda _self: False))
    monkeypatch.setattr(gmail_client.Credentials, "expired", property(lambda _self: True))
    monkeypatch.setattr(gmail_client.Credentials, "refresh", lambda _self, _request: None)
    monkeypatch.setattr(gmail_client.Credentials, "to_json", lambda _self: json.dumps(TOKEN))

    assert load_credentials(paths.token_file) is not None
    assert paths.token_file.stat().st_mode & 0o777 == 0o600
