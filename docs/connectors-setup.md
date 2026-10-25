# Google connectors setup (Gmail, Calendar, Drive, Tasks — all read-only)

The connectors framework (`src/brain/connectors/`) syncs Google sources into
Second Brain as regular items: captured, classified, indexed, searchable,
askable — deduped by a stable external id, incremental via each source's
native cursor. **One Google Cloud project, one OAuth client, one consent, one
refresh token** covers all four sources, because the consent always requests
the UNION of the enabled connectors' scopes (`scopes_for()` in
`src/brain/connectors/google/base.py`, scope table in the same file's
`SCOPE_BY_CONNECTOR`).

Everything here is READ-ONLY (`*.readonly` scopes only); write-back is a later
stage. The token is stored ONLY in the macOS login keychain (service
`second-brain-gmail`, account `default`, defined in
`src/brain/services/keychain.py`; the same item `brain gmail-auth` uses) —
never in the repo, never in `.env`. For the standalone Gmail label poller (an
older path that shares the keychain item), see `docs/email-setup.md`.

> **Pick the right Google account first.** Use the account that actually
> receives the mail you want captured — normally your **personal Gmail**. A
> work or school account is a **Workspace** account: its admin can block the
> OAuth app entirely, and you would be handing work mail to a personal tool.
> Use the same account for every step below, in one browser profile.

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

   - **Publishing status → In production** — this is REQUIRED, not optional
     (see the gotcha section below: in "Testing" the consent itself is
     blocked with 403 `access_denied` unless you add test users, and test-user
     tokens expire every 7 days).
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
   for a personal instance. (A Workspace account may additionally be blocked
   by the org admin regardless of what you configure — see the warning above.)

## The "Testing" publishing-status gotcha (403 access_denied, 7-day tokens)

Two failure modes come from leaving the OAuth consent screen in **Testing**:

- **Consent fails outright** with "Access blocked: <app> has not completed the
  Google verification process" (HTTP 403, error `access_denied`) unless the
  consenting account was added as a **test user** on the consent screen. Your
  own app, your own account — still blocked: "Testing" is a hard gate, not a
  warning you can click through.
- **Tokens expire every 7 days** for test users. Google's OAuth docs state:
  *"A Google Cloud Platform project with an OAuth consent screen configured
  for an external user type and a publishing status of 'Testing' is issued a
  refresh token expiring in 7 days, unless the only OAuth scopes requested are
  a subset of name, email address, and user profile."* Source:
  <https://developers.google.com/identity/protocols/oauth2#expiration>
  (verified 23 Oct 2026).

This project requests `gmail.readonly` (restricted), `calendar.readonly`,
`drive.readonly`, `tasks.readonly` (sensitive) — well beyond name/email/profile
— so **publish the app as "In production"** (*Audience → Publish app →
Confirm*): the refresh token then does not expire on a schedule. An unverified
production app is fine here — see the consent walkthrough below for the exact
screens. If you deliberately stay in "Testing" instead, add your account as a
test user (or consent is blocked with 403 `access_denied`) and re-run
`brain connectors auth google` (one consent, all sources) every week.

## Enable + configure the connectors (`.env`, gitignored)

Settings that exist on main **today** (merged connectors base, PR #53), with
their definitions:

```bash
# Which connectors run (comma-separated, canonical names; "gmail" is accepted
# as an alias for "email" — the alias lives in google/base.py's
# _CONNECTOR_ALIASES and in the registry alias map in connectors/__init__.py).
# Default empty = none enabled (Settings.brain_connectors in config.py);
# unknown names fail fast at startup (the brain_connectors field validator in
# config.py) instead of silently disabling a source.
BRAIN_CONNECTORS="email,gcal,gdrive,gtasks"

# The Desktop OAuth client secret from step 4 (outside the repo). Reads as
# GMAIL_CREDENTIALS_PATH -> Settings.gmail_credentials_path in config.py;
# read by gmail-auth, the poller and `brain connectors auth`.
GMAIL_CREDENTIALS_PATH=~/.config/second-brain/gmail-client-secret.json
```

Per-connector settings below land **with each connector's PR** (each name is
added to `src/brain/config.py` by that PR; none of them resolve on main
alone):

```bash
# Calendar (PR #55): comma-separated calendar ids (allowlist).
# "primary" = the main calendar of the authenticated account. An empty value
# syncs nothing. Find a calendar id: Google Calendar → settings → the
# calendar → "Integrate calendar" → Calendar ID.
BRAIN_GCAL_CALENDARS="primary"

# Drive (PR #57): comma-separated immutable folder ids. EMPTY (default)
# = the Drive connector makes no calls at all. The id is the string after
# /folders/ in the folder's URL (display names are not ids). Two optional
# knobs arrive with the same PR: BRAIN_GDRIVE_DEPTH (subfolder levels walked,
# default 1, hard cap 5) and BRAIN_GDRIVE_TEXT_CAP (exported text cap in
# chars, default 50000).
BRAIN_GDRIVE_FOLDERS="1a2B3c4D5e6F7g8H9i0J"

# Tasks (PR #56): optional allowlist of task lists — ids or titles
# (titles matched case-insensitively). EMPTY = every list. Prefer ids (titles
# can be renamed); an allowlist change invalidates the sync cursor (full
# resync).
BRAIN_GTASKS_LISTS=""
```

Availability proof for what is on main now: `BRAIN_CONNECTORS` →
`Settings.brain_connectors` + its fail-fast field validator and
`KNOWN_CONNECTOR_NAMES` (all in `src/brain/config.py`); the `gmail` alias →
`_CONNECTOR_ALIASES` in `src/brain/connectors/google/base.py` + `alias()` in
`src/brain/connectors/__init__.py`; the scope union → `scopes_for()` in
`src/brain/connectors/google/base.py`; keychain service/account →
`src/brain/services/keychain.py`. The base
framework registers **no Google connector by itself** —
`src/brain/connectors/__init__.py` skips the four adapter modules until they
merge — so on main alone `brain connectors list` shows an empty registry and
`brain connectors sync <name>` exits 78 with "enabled but not available on
this install" (the not-available branch of the sync command in
`src/brain/cli.py`). Each connector ships in its own
PR: Gmail #59, Calendar #55, Drive #57, Tasks #56 — together with the
per-connector settings above. Every setting name in this file will exist in
code on the branch that ships it; the PR description carries the file:line
grep proof per branch.

## First run (order matters)

Install the Google libs **keeping the extras the live install already has**
(each named extra exists in `pyproject.toml`; installing only `.[gmail]`
would drop `dev` and `telegram` from the environment):

```bash
uv pip install -e '.[dev,telegram,gmail]'
```

Works on main **today** (the consent is scope-only — it does not need the
adapters registered):

```bash
# 1. consent — opens a browser; requests the UNION of enabled scopes and
#    stores ONLY the refresh token in the login keychain. The BRAIN_CONNECTORS
#    value here only widens the scope ask (the union); it enables nothing:
BRAIN_CONNECTORS=email,gcal,gdrive,gtasks \
  .venv/bin/brain connectors auth google \
  --client-secret ~/.config/second-brain/gmail-client-secret.json
```

What the browser shows (all expected for your own unverified app):

1. The Google account picker — choose the **same personal Gmail account** the
   setup was done with (see the account warning at the top).
2. **"Google hasn't verified this app"** — expected: the app is yours and you
   are its only user. Click **Advanced** → **Go to <app name> (unsafe)**.
3. The permission list — all four entries must say view/read. If any says
   send, edit or delete, stop: a scope is wrong, recheck step 3 of the setup.
4. After consent, macOS may prompt to access the **login keychain** — click
   **Always Allow**, or the background agents cannot read the token later.

Then check state (hand-run live commands are pinned to the live store
explicitly — the app refuses to touch it without `BRAIN_ALLOW_LIVE=1`, and the
live Qdrant collection is `QDRANT_COLLECTION=brain_items`):

```bash
# state of every connector: enabled, last ok, cursor present, last error:
BRAIN_ALLOW_LIVE=1 QDRANT_COLLECTION=brain_items brain connectors list
```

`auth` refuses to run until `BRAIN_CONNECTORS` is set (the "no connectors
enabled" branch of `brain connectors auth` in `src/brain/cli.py`) and needs
the client secret (the "no client secret" branch right below it). Requires
`BRAIN_CONNECTORS="email,gcal,gdrive,gtasks"` in `.env` so the union covers
all four sources in one consent.

These become usable as each connector's PR merges (on main alone they exit 78
with "enabled but not available on this install" — the not-available branch of
`brain connectors sync` in `src/brain/cli.py`):

```bash
# 2. rehearsal — prints what a sync WOULD capture; writes nothing at all
#    (not even the cursor):
brain connectors sync gcal --dry-run
brain connectors sync email --dry-run --limit 20

# 3. real sync (one cycle; safe to repeat — idempotent by external id+version):
brain connectors sync gcal
brain connectors sync email --limit 100
```

Consent covers every enabled connector at once. If Google returns no refresh
token, revoke the app at <https://myaccount.google.com/permissions> and run
the auth command again. Enabling a connector LATER (add it to
`BRAIN_CONNECTORS`) means one more consent: re-run `brain connectors auth
google` and the requested scope set becomes the new union — the stored token
is replaced, no other source needs separate setup.

## Gmail quota: every call is paced, ~200 messages ≈ 20 minutes

Gmail's API budget is **per Google user** (per token), not per process:
"Total Query Cost" allows 100 quota units/min and one list/get call costs
~5 units. The connector therefore paces its consecutive API calls at least
`gmail_min_call_interval_s` apart (default **6.0 s**; `Settings` in
`src/brain/config.py`, applied in `src/brain/connectors/google/gmail.py`) —
one client stays at ≤ 10 calls/min (≤ 50 units), leaving headroom in the same
budget.

Consequences:

- A first Gmail adopt/sync of ~200 messages is ~200 paced calls ≈ **20
  minutes**. That is the designed speed, not a hang.
- A manual `adopt`/`sync` run **next to the gmail-poll agent** spends the
  same user's budget (two clients together approach the 100 units/min limit).
  A transient 403 quota error is retried in-run (backoff clamped at 60 s), so
  the run completes, just slower.

## What "deleted" means

Sources report deletions (a cancelled event, a trashed file, a deleted task).
Second Brain **never hard-deletes**:

- the item stays in the database and is hidden from lists, search and ask;
- if the source un-deletes the record later, the item re-appears
  automatically;
- items YOU deleted in Second Brain are never re-captured on the next sync;
- records that were already deleted upstream are never created in the first
  place.
