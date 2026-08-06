"""The installer that copies a downloaded OAuth client file into place."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.install_client_secret import describe_problem, main

DESKTOP_CLIENT = {
    "installed": {
        "client_id": "147145549306-abc.apps.googleusercontent.com",
        "client_secret": "GOCSPX-value",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}


def write(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def run(argv: list[str], monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["install_client_secret.py", *argv])
    return main()


def test_a_desktop_client_file_is_accepted(tmp_path):
    assert describe_problem(write(tmp_path / "c.json", DESKTOP_CLIENT)) is None


def test_a_secret_only_download_is_rejected_with_a_pointer(tmp_path):
    # The Client secret row's download button yields just the secret value.
    problem = describe_problem(write(tmp_path / "s.json", {"client_secret": "GOCSPX-only"}))

    assert problem is not None
    assert "write_client_secret.py" in problem


def test_a_web_client_is_rejected(tmp_path):
    payload = {"web": {"client_id": "x", "auth_uri": "https://a", "token_uri": "https://t"}}
    problem = describe_problem(write(tmp_path / "w.json", payload))

    assert problem is not None
    assert "Desktop app" in problem


def test_a_client_missing_required_fields_is_rejected(tmp_path):
    payload = {"installed": {"client_id": "x"}}
    problem = describe_problem(write(tmp_path / "m.json", payload))

    assert problem is not None
    assert "auth_uri" in problem


@pytest.mark.parametrize("content", ["{not json", "[]", '"a string"'])
def test_malformed_files_are_rejected(tmp_path, content):
    path = tmp_path / "bad.json"
    path.write_text(content, encoding="utf-8")

    assert describe_problem(path) is not None


def test_installs_with_owner_only_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("GDB_ROOT", str(tmp_path))
    source = write(tmp_path / "download.json", DESKTOP_CLIENT)

    exit_code = run([str(source)], monkeypatch)
    target = tmp_path / "credentials" / "client_secret.json"

    assert exit_code == 0
    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(target.read_text(encoding="utf-8")) == DESKTOP_CLIENT


def test_refuses_to_overwrite_without_force(tmp_path, monkeypatch):
    monkeypatch.setenv("GDB_ROOT", str(tmp_path))
    source = str(write(tmp_path / "download.json", DESKTOP_CLIENT))

    assert run([source], monkeypatch) == 0
    assert run([source], monkeypatch) == 1
    assert run([source, "--force"], monkeypatch) == 0


def test_a_missing_source_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("GDB_ROOT", str(tmp_path))

    assert run([str(tmp_path / "nope.json")], monkeypatch) == 1


def test_autodiscovery_picks_the_newest_usable_file(tmp_path, monkeypatch):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    write(downloads / "client_secret_old.json", DESKTOP_CLIENT)
    write(downloads / "client_secret_bad.json", {"client_secret": "only"})
    newer = write(downloads / "client_secret_new.json", DESKTOP_CLIENT)
    # Set the time rather than relying on write speed to order the files.
    os.utime(newer, (2_000_000_000, 2_000_000_000))

    monkeypatch.setenv("GDB_ROOT", str(tmp_path))
    monkeypatch.setattr("tools.install_client_secret.SEARCH_DIRECTORIES", (downloads,))

    assert run([], monkeypatch) == 0
    assert (tmp_path / "credentials" / "client_secret.json").exists()


def test_autodiscovery_reports_when_nothing_is_usable(tmp_path, monkeypatch):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    write(downloads / "client_secret_bad.json", {"client_secret": "only"})

    monkeypatch.setenv("GDB_ROOT", str(tmp_path))
    monkeypatch.setattr("tools.install_client_secret.SEARCH_DIRECTORIES", (downloads,))

    assert run([], monkeypatch) == 1
    assert not (tmp_path / "credentials" / "client_secret.json").exists()
