# Gmail Draft Builder

Turn a CSV contact list and a message template into Gmail drafts, one per
recipient. Drafts are created, never sent, so every message can be reviewed in
Gmail before it goes out. Any batch can be deleted from the app if the drafts are
not right.

Built for Apollo exports, but any CSV with an email column works. Contacts you have
not unlocked in Apollo are dropped rather than drafted, along with duplicates and
rows with no usable address. Every dropped row is reported with a reason.

## What it does

- Upload a CSV and match name, email, company, and title columns automatically
- Remember every list you upload, so it can be reloaded with your edits intact
- Review contacts in a table, correct any cell, and drop rows you do not want
- Write a subject, message, and sign off with `{{placeholders}}` per contact
- Write in Markdown for bold, italics, lists, and links
- Preview the first few rendered messages before creating anything
- Attach one or more files to every draft in a batch
- Create one Gmail draft per contact
- Skip anyone this account has emailed before, and report when that was
- Keep a do not contact list that is never drafted to
- Delete a whole batch, or selected drafts, when a batch is not good
- Track every message as draft, scheduled to send, sent, or deleted
- Build tracking spreadsheet rows to paste into Google Sheets
- Save a default template and sign off for the next session

## Requirements

- Python 3.10 or newer
- A Google account with Gmail
- A Google Cloud project with the Gmail API enabled (setup below, about 5 minutes)

## Google Cloud setup

The Gmail API needs your own OAuth client. This is a one time setup, about five
minutes. Google reorganized these screens into the Google Auth platform, so older
walkthroughs referring to "APIs and Services, OAuth consent screen" are stale.

1. Open the [Google Cloud console](https://console.cloud.google.com/) and create a
   project, or pick an existing one. The project name does not matter.

2. Enable the Gmail API. Go to
   [APIs and Services, Library](https://console.cloud.google.com/apis/library),
   search for `Gmail API`, and click Enable.

3. Configure the consent screen at
   [Google Auth platform, Branding](https://console.developers.google.com/auth/branding).
   If you see "Google Auth platform not configured yet", click Get Started.
   - App name: anything, for example `Draft Builder`. Only you will see it.
   - User support email: your own address.
   - Audience: External. Internal is only offered on Workspace accounts and
     restricts the app to your organization.
   - Contact information: your own address.
   - Agree to the user data policy and click Create.

4. Add yourself as a test user at
   [Google Auth platform, Audience](https://console.developers.google.com/auth/audience).
   Under Test users, click Add users, enter the Gmail address whose drafts you
   want to create, and Save. Skipping this makes sign in fail with
   `access_denied`, which is the most common setup mistake.

   While the app's publishing status is Testing with an external user type, Google
   expires the refresh token seven days after you consent, so Connect Gmail has to
   be repeated weekly. Clicking Publish app on that page ends that, at the cost of
   the unverified warning at sign in and a cap of 100 users over the project's
   lifetime. Verification is not required below that cap.

5. Create the client at
   [Google Auth platform, Clients](https://console.developers.google.com/auth/clients).
   Click Create Client, set Application type to Desktop app, name it anything,
   and click Create.

6. Get the client credentials onto disk as `credentials/client_secret.json`.
   Either route works.

   Route A, download the file. In the Clients list, use the download icon at the
   right of the client's row. That yields the full client file. Install it with:

   ```bash
   .venv/bin/python tools/install_client_secret.py
   ```

   That finds the newest download, checks it is a Desktop app client rather than a
   Web one, and copies it into place. Pass a path if it is somewhere unusual.

   Note the download icon on the client detail page, in the Client secrets table,
   gives only the secret value rather than a full client file. The installer says
   so if you use that one by mistake.

   Route B, type the two values in. Open the client and copy Client ID and Client
   secret from the detail page, then:

   ```bash
   .venv/bin/python tools/write_client_secret.py
   ```

   This builds the same file from those two values. If the secret shows as masked
   like `****QNZR`, use the copy icon beside it, or click Add secret to create one
   you can read.

   The `credentials/` directory is gitignored either way.

You do not need to add scopes under Data Access. The app asks for the scope it
needs at sign in time, and Google grants it because you are a test user on your
own project.

The app requests two scopes:

- `gmail.compose`, "manage drafts and send emails". Used to create and delete
  drafts. This app never calls the send endpoint.
- `gmail.readonly`, "view your email messages and settings". Used to search sent
  mail for people you have already emailed and to find messages Gmail is holding to
  send later. Only message headers are read, never bodies. The narrower
  `gmail.metadata` scope cannot be used because
  Google does not allow a search query with it, which would mean walking the whole
  mailbox instead.

Google classifies both as restricted, which matters only if you publish the app to
other people. While it stays in testing with you as the test user, no review is
involved.

If you would rather not grant read access, remove `READ_SCOPE` from `GMAIL_SCOPES`
in `app/config.py` and re-authorize. The sent mail search and scheduled message
detection stop working, which means someone with a pending scheduled send could be
drafted to twice. The do not contact list, this app's own history, and the live
draft check all keep working, since listing drafts needs only the compose scope.

Because the app is unverified, Google shows a warning at sign in. Click Advanced,
then "Go to ... (unsafe)". The app is your own code running on your own machine.

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
   it. The token is saved to `credentials/token.json`. Expect to repeat this weekly
   until the app is published, as step 4 of the setup explains.
2. Contacts: choose your CSV. The app reports how many contacts it found, which
   columns it matched, and which rows it dropped and why. Click
   `Show contact table` to check the parsed rows, fix any cell, or remove a row,
   then `Apply changes`.
3. Message: write the subject, message, and sign off. Click a field chip to
   insert a placeholder at the cursor. Tick `Write in Markdown` for formatting.
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

# Report who has been contacted before, without creating drafts
.venv/bin/python -m app.cli history contacts.csv

# Add addresses to the do not contact list
.venv/bin/python -m app.cli block someone@example.com --note "asked to be removed"

# List batches, then delete one
.venv/bin/python -m app.cli batches
.venv/bin/python -m app.cli delete <batch-id>
```

Arguments not given fall back to whatever was saved from the UI.

## The three tabs

`Compose` builds and sends drafts. `Message status` shows what has happened to
every message. `Tracking sheet` builds the rows for the outreach spreadsheet.

## Remembered lists

Every upload is kept, so a list can be reused without finding the file again.
Click `Saved lists` under the file picker, then `Load` on any entry.

A reloaded list comes back with your edits already applied, and every edited cell
is shaded so it is clear what differs from the export. Re-uploading the same file
finds the existing entry by its contents and brings the same edits back, rather
than starting a second copy.

The original upload is stored byte for byte and never overwritten. `Forget edits`
returns a list to exactly what Apollo produced, and `Delete` forgets it entirely.

Saved lists live under `data/library/`, capped at the 50 most recently used.

## Message status

The status tab lists every address this app has drafted to, with what happened to
the message.

| Status | Meaning |
|---|---|
| Draft | Sitting in Gmail, no send time set |
| Scheduled to send | Gmail is holding it for a later time |
| Sent | In sent mail, no answer yet |
| Replied | The contact wrote back |
| Deleted | Created and then deleted, and no longer in Gmail |

A reply is found by looking for a sent thread that also holds inbound mail, since
Gmail keeps a reply in the thread it answers. Only the From header of each message is
read, never a body.

Filter the list with the buttons above it: `They replied`, `Sent, no reply` for the
follow up pile, any single status, or `Re-emailed`. The search box matches an address
or a subject.

Scheduled messages are found through the `in:scheduled` search rather than the
drafts list, because Gmail keeps them out of that list and gives them no label of
their own. That means scheduled messages created by any tool are counted, not only
this app's.

Sent mail has to be checked to tell a sent message from a deleted one, since both
have left the drafts list. Untick that box for a faster read and those rows show as
deleted instead.

## Tracking sheet

The sheet tab builds one row per contact in the loaded CSV, in the column order the
outreach spreadsheet uses:

`Client`, `Status`, `Contact Name`, `Contact Title`, `Contact Email`,
`Contact LinkedIn`, `Re-Emailed?`, `HPL Assignee`, `Notes`,
`Date of most recent contact`

It is laid out as the spreadsheet reads it, with column letters across the top and
numbered rows down the side, so the page and the pasted result look the same. Set
`First sheet row` to the row you are pasting at and the gutter numbers follow.

Everything is filled in where the app has a value. Client comes from the subject
line, which follows `[Harvard Product Lab x CLIENT]`, falling back to the company
column. Status and the contact date come from the mailbox, in the sheet's own
wording: a sent message is `Reached Out`, an answered one is `Replied`. Dates are
written `8/5/2026` to match the column. LinkedIn comes from the CSV. Notes is always
left blank for you.

A contact with no value for a column gets an empty cell rather than being left out,
so the columns stay aligned when pasted.

Status and Re-Emailed are dropdowns in the sheet, so they are dropdowns here, with
the same choices including ones the app cannot work out such as `email failed :(`.
Client, LinkedIn, Assignee, and Notes are free text. `Remember my entries` saves them
against the contact's address, and they come back the next time that address appears
in any list, in `data/tracker.json`.

`Show remembered values` lists everything saved this way, with the file it lives in,
so a value in a row is never a mystery. Forget one address, or all of them, from
there.

`Copy rows` puts the data rows on the clipboard as tab separated text, with no header
line, since they are appended below rows that already exist. In Google Sheets, click
the first cell of the row you are adding to and paste; each value lands in its own
column with no import step. The collapsed box below holds the same text in case the
browser blocks clipboard access, which happens on plain http.

## Editing contacts before drafting

`Show contact table` lists every parsed contact with the email, name, company, and
title in editable cells, alongside the CSV row number so a value can be traced back
to the export. Fix a cell, or use Remove to leave someone out, then click
`Apply changes`.

Edits live in memory next to the parsed CSV. The uploaded file is never rewritten,
so the original export is untouched, and `Discard changes` reloads from it. A
restart clears edits along with the upload.

An edited address is validated the same way an imported one is. A value that is not
usable is rejected and the previous address kept, rather than silently drafting to
something malformed.

## Writing in Markdown

Tick `Write in Markdown` to use formatting in the message:

| Syntax | Result |
|---|---|
| `**bold**` | bold |
| `*italic*` | italic |
| `- item` | bullet list |
| `1. item` | numbered list |
| `[text](https://example.com)` | link |
| `# Heading` | heading |
| `> quoted` | block quote |

Preview then shows two tabs, the formatted result and the Markdown source. Drafts
are sent as two parts: the Markdown source as plain text, and the formatted HTML as
the alternative, so a client that cannot show HTML still gets readable text.

Rendered HTML is sanitized. A message body carries values from the uploaded CSV,
which is third party data, and Markdown passes raw HTML through by default, so a
crafted cell could otherwise inject script into the preview page or the message.
Images, styles, and tables are stripped along with scripts, since mail clients
handle them inconsistently.

If the message looks like it contains Markdown while the option is off, preview says
so rather than quietly sending literal asterisks.

## Never emailing the same person twice

An address is held back when a message has actually reached the person, or when a
draft to them is still waiting in the mailbox. Deleting a draft clears the block,
since nothing was sent and there is no longer a pending message to duplicate.

| Source | What it catches | Cleared by |
|---|---|---|
| Gmail sent mail | Anything sent from this account, including by hand or from a phone | Nothing, a sent message stays sent |
| Scheduled messages | Anyone Gmail is holding a message for | Cancelling the scheduled send |
| Live drafts | Addresses with a draft from an earlier batch still in the mailbox | Deleting the draft |
| Do not contact list | Addresses you added by hand | Removing the line from the file |

Whether an earlier draft is still live is confirmed against Gmail's own draft list,
not the local record. So a draft you deleted directly in Gmail stops blocking, and
one you sent from Gmail keeps blocking because it now appears in sent mail. Listing
drafts needs only the compose scope, so this works even with the sent mail search
turned off.

Click `Check who was emailed before` to see the report without creating anything.
The same report appears after `Create drafts`, listing each held back address, why,
when it was first emailed, and how many messages exist.

This makes a batch safe to redo. If the drafts came out wrong, delete them and run
the same list again with a better template.

The sent mail search costs one Gmail lookup per contact, so a list of several
hundred takes a while. Untick "Never draft to anyone this account has emailed
before" to skip it; the do not contact list and this app's own history still apply.
When a lookup fails, the report says coverage was partial rather than implying an
all clear.

Only message headers are read, never bodies.

The do not contact list lives at `data/do-not-contact.txt`, one address per line,
and can be edited by hand. A comma or two spaces after an address starts a note.
Lines beginning with `#` are ignored.

```
# people who asked to be left alone
ada@engines.example, asked not to be contacted
grace@compilers.example
```

## How deletion stays safe

Each run writes a batch record under `data/batches/` holding the draft ids it
created. Deleting a batch deletes only those ids, so drafts you wrote yourself are
never touched. Deleted drafts stay in the record, struck through, as a history of
the run, and no longer hold their address back.

`Remove record` forgets a batch in this app without deleting anything in Gmail.
Because the record is what links a draft id to an address, forgetting it also
forgets that those addresses have a draft waiting, so a later run can draft to them
again and leave two drafts for the same person. Delete the drafts first, or leave
the record in place. Anyone already in your sent mail stays blocked either way.

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
does not need Gmail credentials. Point it at a throwaway data directory rather than
your everyday one, so nothing it writes lands in real tracking data:

```bash
.venv/bin/python -m app.web --port 5099 --root /tmp/gdb-scratch &
.venv/bin/python tools/smoke_test.py http://127.0.0.1:5099 --allow-writes
```

Without `--allow-writes` the endpoints that save values are skipped, which is what
you want when checking the server you actually use.

| Module | Purpose |
|---|---|
| `app/web.py` | Flask app and JSON endpoints |
| `app/cli.py` | Command line interface |
| `app/contacts.py` | CSV parsing and column matching |
| `app/templating.py` | Placeholder substitution |
| `app/history.py` | Prior contact checks and the do not contact list |
| `app/library.py` | Remembered uploads and the edits saved against them |
| `app/mailbox.py` | Reading draft, scheduled, and sent state from Gmail |
| `app/markup.py` | Markdown rendering and HTML sanitizing |
| `app/tracker.py` | Tracking spreadsheet rows and remembered field values |
| `app/drafts.py` | Rendering and batch create and delete |
| `app/gmail_client.py` | OAuth and Gmail draft calls |
| `app/message.py` | MIME assembly |
| `app/store.py` | Settings and batch records on disk |
| `tools/install_client_secret.py` | Install a downloaded OAuth client file |
| `tools/write_client_secret.py` | Build `client_secret.json` without a download |
| `tools/smoke_test.py` | Check a running server over HTTP |

## Notes on scope

The server binds to loopback and has no login, so anyone who can reach the port
can use the connected Gmail account. Do not expose it to a network you do not
control.

Uploaded CSVs and staged attachments are held in memory for the life of the
process. Restarting the server clears them; saved templates and batch records
persist on disk.

`DEPLOYMENT.md` covers sharing this with a team, including why each person running
their own copy beats hosting one.
