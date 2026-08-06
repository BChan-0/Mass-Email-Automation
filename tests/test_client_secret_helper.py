"""The helper that writes credentials/client_secret.json by hand."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import GMAIL_SCOPES
from tools.write_client_secret import build_config, main

CLIENT_ID = "1234567890-abcdef.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-test_secret_value"


def test_config_has_the_installed_app_shape():
    config = build_config(CLIENT_ID, CLIENT_SECRET)

    assert set(config) == {"installed"}
    assert config["installed"]["client_id"] == CLIENT_ID
    assert config["installed"]["client_secret"] == CLIENT_SECRET


def test_generated_config_is_accepted_by_the_oauth_library(tmp_path):
    # The real check: google-auth-oauthlib has to load this without complaint.
    path = tmp_path / "client_secret.json"
    path.write_text(json.dumps(build_config(CLIENT_ID, CLIENT_SECRET)), encoding="utf-8")

    flow = InstalledAppFlow.from_client_secrets_file(str(path), GMAIL_SCOPES)
    url, _ = flow.authorization_url(prompt="consent")

    assert "gmail.compose" in url
    assert CLIENT_ID in url


def test_writes_the_file_with_owner_only_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("GDB_ROOT", str(tmp_path))

    exit_code = main_with(["--client-id", CLIENT_ID, "--client-secret", CLIENT_SECRET], monkeypatch)
    target = tmp_path / "credentials" / "client_secret.json"

    assert exit_code == 0
    assert target.exists()
    assert target.stat().st_mode & 0o777 == 0o600


def test_refuses_to_overwrite_without_force(tmp_path, monkeypatch):
    monkeypatch.setenv("GDB_ROOT", str(tmp_path))
    arguments = ["--client-id", CLIENT_ID, "--client-secret", CLIENT_SECRET]

    assert main_with(arguments, monkeypatch) == 0
    assert main_with(arguments, monkeypatch) == 1
    assert main_with([*arguments, "--force"], monkeypatch) == 0


def test_rejects_a_value_that_is_not_a_client_id(tmp_path, monkeypatch):
    monkeypatch.setenv("GDB_ROOT", str(tmp_path))

    exit_code = main_with(["--client-id", "pasted-the-wrong-thing", "--client-secret", CLIENT_SECRET], monkeypatch)

    assert exit_code == 1
    assert not (tmp_path / "credentials" / "client_secret.json").exists()


def test_rejects_a_truncated_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("GDB_ROOT", str(tmp_path))

    assert main_with(["--client-id", CLIENT_ID, "--client-secret", "short"], monkeypatch) == 1


def main_with(argv: list[str], monkeypatch) -> int:
    """Run the helper's main with a given argument list."""
    monkeypatch.setattr(sys, "argv", ["write_client_secret.py", *argv])
    return main()
