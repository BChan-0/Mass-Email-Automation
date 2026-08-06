"""Install a downloaded OAuth client file as credentials/client_secret.json.

The console's download button produces a file whose name varies, so this finds the
newest matching file in Downloads, checks it is really an installed app client, and
copies it into place.

Usage:
    python tools/install_client_secret.py
    python tools/install_client_secret.py ~/Downloads/client_secret_123.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import paths_from_env

SEARCH_DIRECTORIES = (Path.home() / "Downloads", Path.home() / "Desktop", Path.home())
FILENAME_PATTERNS = ("client_secret*.json", "*googleusercontent*.json", "credentials*.json")

# google-auth-oauthlib needs these three inside the installed or web block.
REQUIRED_KEYS = ("client_id", "auth_uri", "token_uri")


def find_candidates() -> list[Path]:
    """Find likely client secret downloads, newest first."""
    found: dict[Path, float] = {}
    for directory in SEARCH_DIRECTORIES:
        if not directory.is_dir():
            continue
        for pattern in FILENAME_PATTERNS:
            for path in directory.glob(pattern):
                if path.is_file():
                    found[path] = path.stat().st_mtime
    return sorted(found, key=lambda path: found[path], reverse=True)


def describe_problem(path: Path) -> str | None:
    """Return why this file cannot be used, or None when it is usable.

    :param path: candidate file downloaded from the console
    :returns: a message naming the problem, or None
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return f"not readable JSON ({error})"

    if not isinstance(payload, dict):
        return "not a JSON object"
    if "installed" not in payload and "web" not in payload:
        # The Client secret row's download can yield just the secret value.
        if "client_secret" in payload or "clientSecret" in payload:
            return (
                "this holds only the client secret, not a full client file. "
                "Use tools/write_client_secret.py instead, or download from the client row"
            )
        return "missing the installed or web block, so it is not an OAuth client file"

    block = payload.get("installed") or payload.get("web") or {}
    missing = [key for key in REQUIRED_KEYS if not block.get(key)]
    if missing:
        return f"missing required field(s): {', '.join(missing)}"
    if "web" in payload and "installed" not in payload:
        return (
            "this is a Web application client. Create a client whose type is "
            "Desktop app, which is what a loopback sign in needs"
        )
    return None


def main() -> int:
    """Locate or accept a downloaded client file and install it."""
    parser = argparse.ArgumentParser(description="Install a downloaded OAuth client file.")
    parser.add_argument("source", nargs="?", default=None, help="path to the downloaded JSON")
    parser.add_argument("--force", action="store_true", help="replace an existing file")
    arguments = parser.parse_args()

    paths = paths_from_env()
    paths.ensure()
    target = paths.client_secret_file

    if target.exists() and not arguments.force:
        print(f"{target} already exists. Re-run with --force to replace it.", file=sys.stderr)
        return 1

    if arguments.source:
        source = Path(arguments.source).expanduser()
        if not source.is_file():
            print(f"No file at {source}.", file=sys.stderr)
            return 1
    else:
        candidates = find_candidates()
        usable = [path for path in candidates if describe_problem(path) is None]
        if not usable:
            print("No usable OAuth client file found in Downloads, Desktop, or your home directory.")
            for path in candidates[:5]:
                print(f"  skipped {path}: {describe_problem(path)}")
            print("\nPass the path directly, or type the values in with:")
            print("  .venv/bin/python tools/write_client_secret.py")
            return 1
        source = usable[0]
        print(f"Found {source}")

    problem = describe_problem(source)
    if problem is not None:
        print(f"Cannot use {source}: {problem}", file=sys.stderr)
        return 1

    payload = json.loads(source.read_text(encoding="utf-8"))
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    target.chmod(0o600)

    client_id = (payload.get("installed") or {}).get("client_id", "")
    print(f"Installed as {target}")
    print(f"Client ID: {client_id}")
    print("Now click Connect Gmail in the app, or run: python -m app.cli auth")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
