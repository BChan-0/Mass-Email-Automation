"""Assemble credentials/client_secret.json from a Client ID and client secret.

Use this when the Google Cloud console does not offer a JSON download for the
OAuth client, or the download is awkward to reach. The file this writes is the
same shape the console produces for a Desktop app client.

Both values are on the client's detail page in the console: Google Auth platform,
Clients, then click the client name.

Usage:
    python tools/write_client_secret.py
    python tools/write_client_secret.py --client-id ID --client-secret SECRET
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import paths_from_env  # noqa: E402

AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
CERT_URI = "https://www.googleapis.com/oauth2/v1/certs"

# Desktop clients created in the console end with this suffix.
EXPECTED_ID_SUFFIX = ".apps.googleusercontent.com"


def build_config(client_id: str, client_secret: str) -> dict:
    """Build the installed app client configuration.

    :param client_id: OAuth client ID from the console
    :param client_secret: OAuth client secret from the console
    :returns: config in the shape google-auth-oauthlib expects
    """
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": AUTH_URI,
            "token_uri": TOKEN_URI,
            "auth_provider_x509_cert_url": CERT_URI,
            "redirect_uris": ["http://localhost"],
        }
    }


def main() -> int:
    """Prompt for the two values, validate them, and write the file."""
    parser = argparse.ArgumentParser(description="Write credentials/client_secret.json by hand.")
    parser.add_argument("--client-id", default=None, help="OAuth client ID")
    parser.add_argument("--client-secret", default=None, help="OAuth client secret")
    parser.add_argument("--force", action="store_true", help="overwrite an existing file")
    arguments = parser.parse_args()

    paths = paths_from_env()
    paths.ensure()
    target = paths.client_secret_file

    if target.exists() and not arguments.force:
        print(f"{target} already exists. Re-run with --force to replace it.", file=sys.stderr)
        return 1

    client_id = arguments.client_id or input("Client ID: ").strip()
    client_secret = arguments.client_secret or input("Client secret: ").strip()

    if not client_id or not client_secret:
        print("Both the client ID and the client secret are required.", file=sys.stderr)
        return 1
    if not client_id.endswith(EXPECTED_ID_SUFFIX):
        print(
            f"That client ID does not end with {EXPECTED_ID_SUFFIX}, so it is probably "
            "not a Client ID. Copy the value labelled Client ID on the client detail page.",
            file=sys.stderr,
        )
        return 1
    if len(client_secret) < 12:
        print("That client secret looks too short. Copy the whole value.", file=sys.stderr)
        return 1

    target.write_text(json.dumps(build_config(client_id, client_secret), indent=2) + "\n", encoding="utf-8")
    target.chmod(0o600)

    print(f"Wrote {target}")
    print("Now click Connect Gmail in the app, or run: python -m app.cli auth")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
