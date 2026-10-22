# Email source setup (Gmail label → Second Brain)

The email source polls ONE Gmail label every 60 seconds (read-only) and
captures each new message through the same paste/parse path as every other
capture (source `email`, classified by Jev, indexed, searchable, askable).
Messages are deduped by their RFC 5322 Message-ID, so re-polling or
re-running can never double-capture.

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

## Run it 24/7

The launchd agent wiring (a third `com.secondbrain.email` agent in
`scripts/service.sh`) is a deliberate follow-up; for now run
`brain gmail-poll` in a terminal (or a tmux window). The poller is
idempotent — stopping and restarting it never re-captures old mail.
