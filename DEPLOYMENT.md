# Sharing this with a team

A plan for letting a handful of people in one organization use this, at no cost.
Nothing here is deployed yet.

## What the size of the team decides

Google classes both scopes this app uses, `gmail.compose` and `gmail.readonly`, as
restricted. That sounds like it forces a verification review, and above 100 users it
does. Below 100 it does not: Google exempts apps used by
[fewer than 100 users](https://support.google.com/cloud/answer/13464323), which sign
in past an unverified app warning.

For a team of five to fifteen, no verification and no security assessment is needed.
The assessment is only triggered for an app that reaches user data
[from or through a third-party server](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification),
which is a reason to keep this off a server, covered below.

## Hosting it so people sign in with their own Gmail

This is four pieces of work, not a hosting setting, because the app has no concept of
a user today.

### 1. A Web application OAuth client, not a Desktop one

`run_local_authorization` calls `flow.run_local_server`, which opens a browser on
whichever machine runs it and listens on a loopback port. On a server that authorizes
nobody. A hosted copy needs the web server flow: send the user to Google, take the code
back on a redirect route, and exchange it.

Google's rules constrain the address before any code is written. Redirect URIs
[must use HTTPS and cannot be raw IP addresses](https://developers.google.com/identity/protocols/oauth2/web-server),
with localhost the only exemption, so a hosted copy needs a real domain and a
certificate. The redirect must match the registered one exactly, including scheme,
case, and trailing slash. To get a refresh token the request needs
`access_type=offline`, and it comes back only on the first consent, so it has to be
stored rather than re-derived.

### 2. Sign in, and a token per person

`Paths.token_file` is a single `credentials/token.json`, and nothing in `app/` knows
what a user is. Hosted as it stands, everyone would share whichever mailbox connected
last. That needs a session tied to the Google account id rather than the address, one
token file per user, and every path that reads `token_file` taking the signed in user.
The same split applies to `data/`: batches, saved lists, tracker values, and the do not
contact list are all per person today and would otherwise be shared.

### 3. Somewhere to keep secrets and state

The client secret cannot sit in the repo, which `.gitignore` currently enforces, so it
moves to an environment variable. `app.secret_key` is regenerated on every start, which
would log everyone out on each deploy, so it becomes configured. Tokens are the
sensitive part: a host holding fifteen refresh tokens is worth attacking in a way one
laptop is not, so they want encrypting at rest rather than sitting as plain JSON.

### 4. Google's user cap, and the warning screen

Verification is not needed below 100 users, but every one of them still sees the
unverified warning and clicks through Advanced. The cap is
[100 users for the lifetime of the project](https://support.google.com/cloud/answer/15549945)
and cannot be reset.

Hosting does change one thing: the security assessment applies to an app reaching
restricted scope data from or through a third-party server. Local copies are out of
scope by definition; a hosted copy is in scope the moment verification is needed.

## The cheaper answer: each person runs it

Every teammate installs the app on their own machine and connects their own mailbox.
This already gets them signing in with their own Gmail, today, with no server, no
per-user token storage to get wrong, and no assessment exposure.

Running locally keeps each person's Gmail token on their own disk at mode 0600, rather
than collecting fifteen refresh tokens onto one host.

The one thing local copies cannot do by themselves is tell you what a teammate has
already sent. That is the part worth building, and it does not need a server.

If you want the hosted version regardless, the four pieces above are the work. They are
worth doing deliberately rather than as a patch.

## Setup for the team

One shared Cloud project, not one per person. That removes the console walkthrough
from everyone except whoever sets it up.

1. In the existing Cloud project, keep the Desktop client already created.
2. On [Audience](https://console.developers.google.com/auth/audience), click
   Publish app. While the status is Testing with an external user type, Google
   expires each refresh token
   [seven days after consent](https://developers.google.com/identity/protocols/oauth2),
   so every teammate would reconnect weekly. Publishing ends that. It does not
   require verification below 100 users; it keeps the unverified warning, and the
   100 user budget then applies across the project's lifetime.
3. Share `credentials/client_secret.json` with the team over something private.
   Google treats an installed app client secret as
   [not actually secret](https://developers.google.com/identity/protocols/oauth2),
   because the app ships to users, so this is expected rather than a shortcut.
4. Each person clones the repo, runs `./run.sh`, drops the file into `credentials/`,
   and clicks Connect Gmail once.

Worth one question to Harvard IT before settling: if a Harvard owned Google Cloud
organization is available to student groups, an Internal user type app is
[exempt from both the warning screen and the 100 user cap](https://support.google.com/cloud/answer/13464323).
That is the cleanest answer available, and it costs one email to find out.

## Shared contact tracking

Implemented on the `shared-team-tracking` branch, not wired into the UI and not
running anywhere. See `app/shared.py`.

A CSV in a folder the team already shares, such as a synced Drive folder. Each copy
reads it to learn who is taken, and appends its own outreach to it. The log holds an
address, an owner, a status, and a date. No credentials and no message bodies, so a
shared folder is an appropriate place for it.

Writes are append only, one row per action, and the newest row for an address wins
on read. Two people saving at the same moment therefore produce two rows rather than
one overwriting the other, which matters because a synced folder offers no locking.

`ContactGuard` accepts these claims and checks them before it touches Gmail, so a
contact a teammate already owns is skipped without spending an API call.

### The alternative worth considering

Writing straight into the team's existing Google Sheet through the Sheets API. The
`spreadsheets` scope is sensitive rather than restricted, so it does not raise the
verification tier this app already sits in, and the free quota is far above what a
team of this size would use. It would put the shared state where the team already
looks, instead of in a second file.

The Sheets API has no conditional write or revision check, so the same append only
discipline would apply. This is a reasonable next step rather than a replacement for
the CSV, which needs no extra scope and no re-consent.

## Free hosting, if the team ever outgrows local copies

Recorded so the comparison does not have to be redone. Every option below was
checked for whether the filesystem survives a restart, since this app keeps tokens
and batch records on disk.

| Option | Free | Disk survives restart | Notes |
|---|---|---|---|
| Cloud Run | Yes, card required | No | In memory filesystem, scales to zero |
| Render | Yes, no card | No | Free services cannot attach a disk, sleeps when idle |
| PythonAnywhere | Yes, no card | Yes, 512 MB | Egress goes through a proxy, which the Google client may not traverse |
| Oracle Always Free | Yes, card required | Yes, 200 GB | Reclaims instances that stay idle, so needs keeping warm |
| Fly.io | No longer free | Yes, paid volumes | Around 2 to 3 dollars a month |
| Self host | Yes | Yes | Needs a domain and TLS, since a redirect URI cannot be a raw IP |

Any of these still needs the multi-user work listed above, plus a real domain: Google
requires a redirect URI to use HTTPS and refuses raw IP addresses. That is the second
reason hosting is the expensive answer to a problem the shared log already solves.
