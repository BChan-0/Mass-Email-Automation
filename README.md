# Gmail Draft Builder

Turn a CSV contact list and a message template into Gmail drafts, one per
recipient. Drafts are created, never sent, so every message can be reviewed in
Gmail before it goes out. Any batch can be deleted from the app if the drafts are
not right.

Built for Apollo exports, but any CSV with an email column works.

## What it does

- Upload a CSV and match name, email, company, and title columns automatically
- Write a subject, message, and sign off with `{{placeholders}}` per contact
- Preview the first few rendered messages before creating anything
- Attach one or more files to every draft in a batch
- Create one Gmail draft per contact
- Delete a whole batch, or selected drafts, when a batch is not good
- Save a default template and sign off for the next session

## Requirements

- Python 3.10 or newer
- A Google account with Gmail
- A Google Cloud project with the Gmail API enabled (setup below, about 5 minutes)

## Google Cloud setup

The Gmail API needs an OAuth client. This is a one time setup.

1. Open the [Google Cloud console](https://console.cloud.google.com/) and create a
   project, or pick an existing one.
2. Enable the Gmail API: APIs and Services, Library, search for Gmail API, Enable.
3. Configure the OAuth consent screen: APIs and Services, OAuth consent screen.
   - User type: External is fine for a personal account.
   - Fill in the app name and your email.
   - Under Audience, add your own Gmail address as a test user. Without this,
     sign in fails with `access_denied`.
4. Create the client: APIs and Services, Credentials, Create Credentials,
   OAuth client ID.
   - Application type: Desktop app.
5. Download the JSON and save it as `credentials/client_secret.json` in this
   repo. The `credentials/` directory is gitignored.

The app requests only the `gmail.compose` scope, which allows creating and
deleting drafts. It cannot read your inbox.

## Install and run

```bash
git clone <your-repo-url> gmail-draft-builder
cd gmail-draft-builder
./run.sh
```

`run.sh` creates a virtualenv and installs dependencies on first run, then serves
the UI at http://127.0.0.1:5000.

To install by hand instead:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.web
```

Useful flags: `--port 8080`, `--host`, `--root DIR` to keep data and credentials
somewhere other than the repo. `GDB_ROOT` does the same as `--root`.

## Using the UI

1. Click `Connect Gmail`. A browser window opens for Google sign in. Google will
   warn that the app is unverified because it is your own client; continue past
   it. The token is saved to `credentials/token.json`.
2. Contacts: choose your CSV. The app reports how many contacts it found, which
   columns it matched, and which rows it dropped and why.
3. Message: write the subject, message, and sign off. Click a field chip to
   insert a placeholder at the cursor.
4. Attachments: add files to include in every draft in the batch.
5. Preview: render the first few messages and check them.
6. Create drafts: one draft per contact. Nothing is sent.
7. Batches: each run is listed with its drafts. Delete all of them, or tick
   individual rows and delete only those.

Open Gmail and look in Drafts to send them.

## Placeholders

Wrap a field name in double braces:

```
Hi {{first_name}}, I saw your work as {{title}} at {{company}}.
```

Add a fallback after a pipe for contacts missing that field:

```
Hi {{first_name|there}},
```

Always available:

| Placeholder | Value |
|---|---|
| `{{first_name}}` | First name, split from a single name column if needed |
| `{{last_name}}` | Last name |
| `{{full_name}}` | First and last name |
| `{{email}}` | Email address |
| `{{company}}` | Company or organization |
| `{{title}}` | Job title |
| `{{sender_name}}` | Your name, from the Your name field |
| `{{signoff}}` | The rendered sign off, if you want it mid message |

Every other CSV column is available under its lowercased header with underscores,
so a column named `Seniority` becomes `{{seniority}}`.

By default a contact missing a field the template uses is skipped rather than
drafted with visible `{{braces}}`. Skipped contacts are listed after the run.
Turn this off under Cc, Bcc, and format.

The sign off is appended to the end of the message. If the message body already
contains `{{signoff}}`, it is placed there instead and not repeated.

## Command line

Same operations without a browser.

```bash
# Authorize once
.venv/bin/python -m app.cli auth

# Render without touching Gmail
.venv/bin/python -m app.cli preview samples/contacts-sample.csv --limit 2

# Create drafts, with an attachment
.venv/bin/python -m app.cli create contacts.csv \
  --subject "Quick question about {{company}}" \
  --body-file message.txt \
  --sender-name "Your Name" \
  --attach deck.pdf

# List batches, then delete one
.venv/bin/python -m app.cli batches
.venv/bin/python -m app.cli delete <batch-id>
```

Arguments not given fall back to whatever was saved from the UI.

## How deletion stays safe

Each run writes a batch record under `data/batches/` holding the draft ids it
created. Deleting a batch deletes only those ids, so drafts you wrote yourself are
never touched. Deleted drafts stay in the record, struck through, as a history of
the run.

`Remove record` forgets a batch in this app without deleting anything in Gmail.

## Limits

- 2000 contacts per batch, 20 MB per attachment, 25 MB per CSV
- Gmail allows 25 MB per message including attachments
- Draft creation is one API call per contact, so large lists take a few minutes
- Gmail rate limits are retried with backoff; if the first five contacts all fail
  the run stops rather than working through a list that cannot succeed

## Files not to commit

`credentials/` and `data/` are gitignored. `credentials/` holds your OAuth client
secret and access token; `data/` holds uploaded contacts and batch records. Check
`git status` before your first push if you moved anything.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format .
```

Tests use a Gmail stub in `tests/conftest.py` and never reach the network.

`tools/smoke_test.py` checks a running server over HTTP, covering everything that
does not need Gmail credentials:

```bash
.venv/bin/python -m app.web --port 5099 &
.venv/bin/python tools/smoke_test.py http://127.0.0.1:5099
```

| Module | Purpose |
|---|---|
| `app/web.py` | Flask app and JSON endpoints |
| `app/cli.py` | Command line interface |
| `app/contacts.py` | CSV parsing and column matching |
| `app/templating.py` | Placeholder substitution |
| `app/drafts.py` | Rendering and batch create and delete |
| `app/gmail_client.py` | OAuth and Gmail draft calls |
| `app/message.py` | MIME assembly |
| `app/store.py` | Settings and batch records on disk |

## Notes on scope

The server binds to loopback and has no login, so anyone who can reach the port
can use the connected Gmail account. Do not expose it to a network you do not
control.

Uploaded CSVs and staged attachments are held in memory for the life of the
process. Restarting the server clears them; saved templates and batch records
persist on disk.
