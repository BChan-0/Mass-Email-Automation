"""Exercise a running server over HTTP.

Covers everything that does not need Gmail credentials: uploads, preview,
settings, and the error paths. Draft creation is checked by the pytest suite,
which stubs Gmail.

Usage: python tools/smoke_test.py [base-url]
"""

from __future__ import annotations

import io
import json
import mimetypes
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000"
SAMPLE_CSV = Path(__file__).resolve().parent.parent / "samples" / "contacts-sample.csv"

# Flask's session cookie has to be carried across requests so the server can
# associate an upload with the later preview call.
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(), urllib.request.ProxyHandler({}))

passed = 0
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one assertion."""
    global passed
    if condition:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed.append(name)
        print(f"  FAIL {name}{f': {detail}' if detail else ''}")


def request(method: str, path: str, *, body: bytes | None = None, content_type: str = "") -> tuple[int, dict]:
    """Send a request and decode the JSON response."""
    outgoing = urllib.request.Request(f"{BASE}{path}", data=body, method=method)
    if content_type:
        outgoing.add_header("Content-Type", content_type)
    try:
        with opener.open(outgoing, timeout=20) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return error.code, {"error": raw.decode("utf-8", "replace")[:200]}


def post_json(path: str, payload: dict) -> tuple[int, dict]:
    return request("POST", path, body=json.dumps(payload).encode(), content_type="application/json")


def post_file(path: str, filename: str, content: bytes) -> tuple[int, dict]:
    """Post one file as multipart form data."""
    boundary = f"----smoke{uuid.uuid4().hex}"
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    buffer = io.BytesIO()
    buffer.write(f"--{boundary}\r\n".encode())
    buffer.write(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode())
    buffer.write(f"Content-Type: {mime_type}\r\n\r\n".encode())
    buffer.write(content)
    buffer.write(f"\r\n--{boundary}--\r\n".encode())
    return request("POST", path, body=buffer.getvalue(), content_type=f"multipart/form-data; boundary={boundary}")


def main() -> int:
    print(f"Smoke testing {BASE}")

    print("page and status")
    page = urllib.request.Request(f"{BASE}/")
    with opener.open(page, timeout=20) as response:
        html = response.read().decode("utf-8")
    check("index returns HTML", "Gmail Draft Builder" in html)
    check("placeholder syntax is not eaten by Jinja", "{{company}}" in html)

    status_code, status = request("GET", "/api/status")
    check("status responds", status_code == 200)
    check("status reports connection state", "connected" in status)

    print("csv upload")
    code, payload = post_file("/api/upload-csv", "contacts-sample.csv", SAMPLE_CSV.read_bytes())
    check("upload accepted", code == 200, str(payload))
    check("four contacts parsed", payload.get("contact_count") == 4, str(payload.get("contact_count")))
    check("email column detected", payload.get("detected", {}).get("email") == "Email")
    check("locked row skipped", len(payload.get("skipped", [])) == 1)
    check("extra columns exposed", "seniority" in payload.get("available_fields", []))
    csv_id = payload.get("csv_id")

    code, payload = post_file("/api/upload-csv", "bad.csv", b"Name,Company\nAda,Engines\n")
    check("csv without an email column is rejected", code == 400)
    check("rejection explains why", "email column" in payload.get("error", ""))

    print("preview")
    code, payload = post_json(
        "/api/preview",
        {
            "csv_id": csv_id,
            "subject_template": "Quick question about {{company}}",
            "body_template": "Hi {{first_name|there}},\n\nYour work as {{title}} caught my eye.",
            "signoff_template": "Best,\n{{sender_name}}",
            "sender_name": "Bonnie",
            "limit": 3,
        },
    )
    check("preview responds", code == 200, str(payload))
    check("three drafts rendered", len(payload.get("drafts", [])) == 3)
    first = (payload.get("drafts") or [{}])[0]
    check("subject substituted", first.get("subject") == "Quick question about Analytical Engines")
    check("body substituted", "Hi Ada," in first.get("body", ""))
    check("signoff appended", first.get("body", "").endswith("Best,\nBonnie"))
    check("missing title counted", payload.get("incomplete") == 1, str(payload.get("incomplete")))

    code, payload = post_json(
        "/api/preview",
        {"csv_id": csv_id, "subject_template": "Hi {{unknown_field}}", "body_template": "Body"},
    )
    check("unknown placeholder is reported per draft", "unknown_field" in payload["drafts"][0]["missing"])

    print("attachments")
    code, payload = post_file("/api/upload-attachment", "deck.pdf", b"%PDF-1.4 smoke test")
    check("attachment staged", code == 200 and payload["attachments"][0]["filename"] == "deck.pdf")
    code, payload = post_file("/api/upload-attachment", "deck.pdf", b"%PDF-1.4 again")
    check("same filename is replaced not duplicated", len(payload["attachments"]) == 1)
    code, payload = post_file("/api/upload-attachment", "notes.txt", b"hello")
    check("second attachment staged", len(payload["attachments"]) == 2)
    code, payload = post_json("/api/remove-attachment", {"filename": "deck.pdf"})
    check("attachment removed", [item["filename"] for item in payload["attachments"]] == ["notes.txt"])

    print("settings")
    code, payload = post_json("/api/settings", {"sender_name": "Smoke Test", "cc": "cc@example.com"})
    check("settings saved", payload.get("sender_name") == "Smoke Test")
    code, payload = request("GET", "/api/settings")
    check("settings read back", payload.get("cc") == "cc@example.com")

    print("guards")
    code, payload = post_json("/api/create-drafts", {"csv_id": csv_id, "subject_template": "s", "body_template": "b"})
    check("create requires a connection", code == 401, str(payload))
    code, payload = post_json("/api/create-drafts", {"csv_id": csv_id, "subject_template": "  ", "body_template": "b"})
    check("empty subject rejected before Gmail is called", code == 400)
    code, payload = request("GET", "/api/batches/does-not-exist")
    check("unknown batch is a 404", code == 404)
    code, payload = post_json("/api/connect", {})
    check("connect without a client secret explains the fix", code == 400 and "client_secret.json" in payload["error"])

    print(f"\n{passed} passed, {len(failed)} failed")
    for name in failed:
        print(f"  failed: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
