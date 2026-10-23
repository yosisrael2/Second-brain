# Google connectors setup (Gmail, Calendar, Drive, Tasks — all read-only)

The connectors framework (`src/brain/connectors/`) syncs Google sources into
Second Brain as regular items: captured, classified, indexed, searchable,
askable — deduped by a stable external id, incremental via each source's
native cursor. **One Google Cloud project, one OAuth client, one consent, one
refresh token** covers all four sources, because the consent always requests
the UNION of the enabled connectors' scopes (`scopes_for()` in
`src/brain/connectors/google/base.py`).

Everything here is READ-ONLY (`*.readonly` scopes only); write-back is a later
stage. The token is stored ONLY in the macOS login keychain (the same item
`brain gmail-auth` uses) — never in the repo, never in `.env`. For the
standalone Gmail label poller (an older path that shares the keychain item),
see `docs/email-setup.md`.

## One-time Google Cloud setup (all four sources at once)

1. **Create/select a Google Cloud project** at
   <https://console.cloud.google.com/> (any name, e.g. "second-brain").
2. **Enable all four APIs** (*APIs & Services → Library → Enable*), so you
   never have to revisit consent:
   - **Gmail API**
   - **Google Calendar API**
   - **Google Drive API**
   - **Tasks API**
3. **Configure the consent screen** (*APIs & Services → OAuth consent screen*):
   - User type **External**; fill the app name + your email.
   - Add the four read-only scopes (or add them later via
     *Data access → Add or remove scopes*). The full list, in the order
     `scopes_for(["email", "gcal", "gdrive", "gtasks"])` requests them:

     ```
     https://www.googleapis.com/auth/gmail.readonly
     https://www.googleapis.com/auth/calendar.readonly
     https://www.googleapis.com/auth/drive.readonly
     https://www.googleapis.com/auth/tasks.readonly
     ```

   - **Publishing status → In production** (see the 7-day gotcha below).
4. **Create an OAuth client** (*APIs & Services → Credentials → Create
   credentials → OAuth client ID*):
   - Application type: **Desktop app** (the consent flow runs a local loopback
     server; this is the client type both `gmail-auth` and
     `brain connectors auth` expect).
   - **Download the client secret JSON** and keep it OUTSIDE this repo (e.g.
     `~/.config/second-brain/gmail-client-secret.json`). It is a credential;
     never commit it. The same client secret serves `gmail-auth`, the poller,
     and every connector.
5. Only your own Google account can consent unless you add test users — fine
   for a personal instance.

## The "Testing" publishing-status gotcha (7-day tokens)

Google's OAuth docs state: *"A Google Cloud Platform project with an OAuth
consent screen configured for an external user type and a publishing status of
'Testing' is issued a refresh token expiring in 7 days, unless the only OAuth
scopes requested are a subset of name, email address, and user profile."*
Source: <https://developers.google.com/identity/protocols/oauth2#expiration>
(verified 23 Oct 2026).

This project requests `gmail.readonly` (restricted), `calendar.readonly`,
`drive.readonly`, `tasks.readonly` (sensitive) — well beyond name/email/profile
— so **while the app is in "Testing", the refresh token dies after 7 days** and
every source stops until you re-consent. Pick one:

- **Publish as "In production"** (recommended for a personal/internal app):
  the refresh token then does not expire on a schedule. Unverified apps with
  sensitive scopes show Google's "unverified app" warning during your own
  consent; for a personal instance that is cosmetic and you proceed through it.
- **Stay in "Testing"**: everything works, but re-run
  `brain connectors auth google` (one consent, all sources) every week.

## Enable + configure the connectors (`.env`, gitignored)

```bash
# Which connectors run (comma-separated, canonical names; "gmail" is accepted
# as an alias for "email"). Default empty = none enabled; unknown names fail
# fast at startup instead of silently disabling a source.
BRAIN_CONNECTORS="email,gcal,gdrive,gtasks"

# Calendar: comma-separated calendar ids (allowlist). "primary" = the main
# calendar of the authenticated account. An empty value syncs nothing.
# Find a calendar id: Google Calendar → settings → the calendar →
# "Integrate calendar" → Calendar ID.
BRAIN_GCAL_CALENDARS="primary"

# Drive: comma-separated immutable folder ids. EMPTY (default) = the Drive
# connector makes no calls at all. The id is the string after /folders/ in the
# folder's URL (open the folder in Drive, copy it from the address bar —
# display names are not ids). Two optional knobs exist: BRAIN_GDRIVE_DEPTH
# (subfolder levels walked, default 1, hard cap 5) and BRAIN_GDRIVE_TEXT_CAP
# (exported text cap in chars, default 50000).
BRAIN_GDRIVE_FOLDERS="1a2B3c4D5e6F7g8H9i0J"

# Tasks: optional allowlist of task lists — ids or titles (titles matched
# case-insensitively). EMPTY = every list. Prefer ids (titles can be renamed);
# an allowlist change invalidates the sync cursor (full resync).
BRAIN_GTASKS_LISTS=""

# The Desktop OAuth client secret from step 4 (outside the repo):
GMAIL_CREDENTIALS_PATH=~/.config/second-brain/gmail-client-secret.json
```

Note: the Calendar/Drive/Tasks connectors and their settings ship in their own
PRs (C2/C3/C4); this file's branch carries the framework + the Gmail
connector, so on that branch alone only `BRAIN_CONNECTORS` resolves. Every
setting name above exists in code — see the PR description for the file:line
grep proof per branch.

## First run (order matters)

```bash
pip install -e '.[gmail]'                      # google libs (one extra)

# 1. consent — opens a browser; requests the UNION of enabled scopes and
#    stores ONLY the refresh token in the login keychain:
brain connectors auth google --client-secret ~/.config/second-brain/gmail-client-secret.json

# 2. rehearsal — prints what a sync WOULD capture; writes nothing at all
#    (not even the cursor):
brain connectors sync gcal --dry-run
brain connectors sync email --dry-run --limit 20

# 3. real sync (one cycle; safe to repeat — idempotent by external id+version):
brain connectors sync gcal
brain connectors sync email --limit 100

# state of every connector: enabled, last ok, cursor present, last error:
brain connectors list
```

Consent covers every enabled connector at once. If Google returns no refresh
token, revoke the app at <https://myaccount.google.com/permissions> and run
the auth command again. Enabling a connector LATER (add it to
`BRAIN_CONNECTORS`) means one more consent: re-run `brain connectors auth
google` and the requested scope set becomes the new union — the stored token
is replaced, no other source needs separate setup.

## What "deleted" means

Sources report deletions (a cancelled event, a trashed file, a deleted task).
Second Brain **never hard-deletes**:

- the item stays in the database, its meta gains `source_deleted: true`, and
  it is hidden from lists, search and ask exactly like superseded items
  (`superseded_by` set to itself — the PR B mechanism);
- if the source un-deletes the record later, the item is un-hidden
  automatically;
- items YOU deleted in Second Brain are never re-captured on the next sync;
- records that were already deleted upstream are never created in the first
  place.
