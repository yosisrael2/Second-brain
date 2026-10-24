# Email source setup (Gmail label → Second Brain)

The email source polls ONE Gmail label every 60 seconds (read-only) and
captures each new message through the same paste/parse path as every other
capture (source `email`, classified by Jev, indexed, searchable, askable).
Re-polling or re-running can never double-capture: the incremental sync walks
Gmail's history API with a `historyId` cursor, and every captured message is
recorded in the connector ledger (`connector_records`, seeded from the legacy
seen-table) while the ingest-time content fingerprint backstops re-deliveries.
Since the connectors port (C5) the launchd agent, `brain gmail-poll` and
`brain connectors sync email` all drive the SAME connector through the shared
runner — there is one live Gmail path.

Historical note (for the record): before C5, a payload-parser bug meant the
RFC Message-ID was never read from real Gmail payloads, so the fallback
dedupe id hashed (sender=None, subject=None, minute) — two messages arriving
in the SAME minute collided and the second was silently dropped. The parser
is fixed in C5, so a first sync over a mailbox with such history will ingest
those previously dropped mails as new items (correct, but more items than
the old poller ever showed). Gmail has never been consented on live, so no
existing data is affected.

Privacy model:

- The app requests **read-only** Gmail access (`gmail.readonly`) and only
  ever lists messages carrying the one configured label id. It never sends,
  moves, or labels anything in the mailbox.
- The OAuth **refresh token is stored only in the macOS login keychain**
  (service `second-brain-gmail`) — never in the repo, never in `.env`, never
  printed or logged.
- **Attachments leave the machine**: the user approved Claude reading
  images/PDFs. In the attachments step (wired after the attachments core
  lands), image and PDF bytes are fetched and their content is read by
  Claude (the model) to extract meaning. A PDF's text layer is read locally
  first; images/PDF pages still go to the model. Until that step lands,
  F2 stores attachment REFERENCES only (id, filename, mime type, size) in
  the item's meta and fetches nothing.

## One-time Google Cloud setup (user steps)

1. **Create/select a Google Cloud project** at <https://console.cloud.google.com/>
   (any name, e.g. "second-brain").
2. **Enable the Gmail API**: *APIs & Services → Library → "Gmail API" → Enable*.
3. **Configure the consent screen** (*APIs & Services → OAuth consent screen*):
   - User type **External**; fill the app name + your email.
   - Add the scope `.../auth/gmail.readonly` (or add it later via
     *Data access → Add or remove scopes*).
   - **Publish the app** (*Publishing status → In production*). Keeping it in
     "Testing" also works but refresh tokens expire after 7 days and the
     poller will then need re-auth every week — production does not.
   - Only your own Google account will be able to consent unless you add
     test users; that is fine for a personal instance.
4. **Create an OAuth client** (*APIs & Services → Credentials → Create
   credentials → OAuth client ID*):
   - Application type: **Desktop app**.
   - **Download the client secret JSON** and keep it OUTSIDE this repo
     (e.g. `~/.config/second-brain/gmail-client-secret.json`). It is a
     credential; never commit it.
5. **Label your mail in Gmail**: create (or pick) the label whose mail
   should flow into Second Brain. Its **immutable label id** (looks like
   `Label_1234567890`) is what goes into `GMAIL_LABEL_ID` — display names
   are NOT stable identifiers; always use the id. Get the id after the
   consent step (step 6) with the one-liner under "Finding the label id".
6. **Consent once** (opens a browser; log in as the Google account that owns
   the label):

   ```bash
   pip install -e '.[gmail]'
   brain gmail-auth --client-secret ~/.config/second-brain/gmail-client-secret.json
   ```

   This stores ONLY the refresh token in your login keychain.

7. **Configure the poller** in `.env` (gitignored):

   ```bash
   GMAIL_LABEL_ID=Label_1234567890      # the immutable label id
   GMAIL_POLL_INTERVAL_S=60             # default
   # GMAIL_QUERY=newer_than:30d         # optional extra Gmail search filter
   GMAIL_CREDENTIALS_PATH=~/.config/second-brain/gmail-client-secret.json
   ```

8. **Run the poller**:

   ```bash
   brain gmail-poll          # polls every 60s until Ctrl-C
   brain gmail-poll --once   # one cycle (nice for a first smoke test)
   ```

   If the configured id does not exist, the cockpit shows
   `email: label missing` and `brain gmail-poll --once` reports the state.

## Finding the label id (no-code path)

After `gmail-auth`, one API call lists your labels:

```bash
python - <<'PY'
from brain.services.keychain import Keychain, GMAIL_ACCOUNT
from brain.ai.gmail import default_service_factory
svc = default_service_factory(Keychain().read(GMAIL_ACCOUNT), "me")
for l in svc.users().labels().list(userId="me").execute()["labels"]:
    print(l["id"], l["name"])
PY
```

Copy the id (not the name) into `GMAIL_LABEL_ID`.

## Cockpit health states

| State | Meaning | Fix |
| --- | --- | --- |
| `disabled` | `GMAIL_LABEL_ID` unset or no refresh token | set the label id / run `brain gmail-auth` |
| `needs re-auth` | Google rejected the refresh token (revoked, expired, password change) | re-run `brain gmail-auth` |
| `label missing` | the configured label id no longer exists (label deleted) | recreate the label or update `GMAIL_LABEL_ID` |
| `down` | the poller process is not heartbeating | check the poller process / logs |
| `ok` | polling normally | — |

Both `needs re-auth` and `label missing` are DEGRADED, not crashes: the
poller pauses 5 minutes between attempts and everything else keeps working.

## Run it 24/7 (launchd agent)

A third launchd agent keeps the poller alive across logins, same model as
the API and Telegram agents — installed only when `GMAIL_LABEL_ID` is set
(opt-in; without consent there is nothing for the agent to poll, and an
always-down agent would sit in the cockpit):

| Agent | Runs | Restart behaviour |
| --- | --- | --- |
| `com.secondbrain.api` | `scripts/run-server.sh` | 0/78 → no restart, else restart |
| `com.secondbrain.telegram` | `scripts/run-bot.sh` | 0/78 → no restart, else restart |
| `com.secondbrain.email` | `scripts/run-email.sh` | 0/78 → no restart, else restart |

`needs re-auth` and `label missing` are NOT exits: the poller pauses
internally (5 minutes) and the agent stays alive, so a revoked token never
flaps the agent. Config errors that cannot start at all (no label id, no
keychain token) exit 78 → no restart loop.

**Install order matters** — do this from a normal Terminal session FIRST,
while you are at the console:

1. `brain gmail-auth` (browser consent) — the login keychain asks for
   access once; click **Always Allow** so the background agent can read
   the token later without a prompt nobody will see.
2. `brain gmail-poll --once` — verifies label id + token + capture end to
   end, and warms the same keychain grant.
3. `scripts/service.sh install` — writes and bootstraps the agents. The
   email agent is OPT-IN: it is installed only when `GMAIL_LABEL_ID` is
   already set (in the environment or in `.env`); otherwise install prints
   `email: skipped (GMAIL_LABEL_ID not set)` and removes any stale email
   plist, and `service.sh status` shows the email agent as skipped instead
   of down. The email agent runs `scripts/run-email.sh`, which exports
   `BRAIN_ALLOW_LIVE=1` and pins `COMPOSE_PROJECT_NAME=second-brain`.
   Logs: `~/Library/Logs/second-brain/email.log` (poll ids and counts
   only — never message bodies, sender addresses, or tokens).

The poller is idempotent — stopping and restarting it never re-captures
old mail (connector ledger + ingest content-fingerprint dedupe).
