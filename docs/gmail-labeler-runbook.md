# Gmail SB/ Labeler — Runbook (v1)

This runbook lets you run the SB/ labeler **alone, step by step**. It labels
mail in Gmail by **deterministic rules over headers only** — **v1 has no Jev
and no external LLM**, no message text leaves your machine, and it creates
**no Gmail filters** (that is a later version). It is **label-only**: it adds
`SB/…` labels and never removes INBOX, never archives, trashes or deletes
anything, and never touches a Gmail system label (INBOX/UNREAD/TRASH/SPAM/
SENT/DRAFT/STARRED are refused at the client).

Safety rails you are relying on:

- **Dry-run is the default.** Without `--apply` the command performs zero
  write calls and reads only `From`/`Subject`/`List-Id` headers plus label
  ids (metadata GETs) — nothing else.
- `--apply` is explicit and **capped per run**: `--max-apply` (default 20,
  minimum 1 — the parser rejects anything below 1) with a **hard ceiling of
  50** that no flag can lift.
- `--apply --confirm-plan <hash>` applies **exactly the previewed plan**; if
  the mailbox or rules changed since the preview, it refuses and says why.
- Every applied label is written FIRST to a **durable audit ledger** — an
  append-only JSONL file (mode 0600) at
  `~/.second-brain-files/gmail-label-audit.jsonl` — and the label is only
  applied after that write succeeds. The ledger is **not** the ops database
  (`ops_events` is pruned after 7 days, which would silently kill undo);
  the JSONL file lives on, so old batches stay undoable.
- **Account pin:** `GMAIL_LABELER_ACCOUNT_EMAIL` (required) names YOUR
  personal account. `--auth` verifies via Google's `getProfile` that the
  consented mailbox IS that account **before storing the refresh token**
  (the labeler token REPLACES the shared keychain token the mail sync uses —
  a wrong-account consent is refused, never stored), and `--apply` re-checks
  the mailbox before writing anything.
- A message is labeled **once** (already-`SB/`-labeled messages are skipped)
  and gets at most one label. Precedence: Kids > School > Logistics > Bills >
  Noise > Unsorted.
- Message selection uses **the same label + query the email connector syncs**
  (`GMAIL_LABEL_ID` / `GMAIL_QUERY`), so the labeler cannot reach outside the
  pipeline's scope.
- Kid matching is **subject-only, full-token** on confirmed kid names from
  memory — a classmate's name or a substring never triggers the Kids label.

**Pacing** (the 6s minimum interval between Gmail calls, shared with the
poller's token budget): a dry-run scanning 100 messages takes **~10 minutes**
(100 metadata GETs × ~6s); an apply of 20 labels takes **~2 minutes**. That
is the quota hygiene working, not a hang.

**Live-store guard:** the command refuses the LIVE store unless
`BRAIN_ALLOW_LIVE=1` — that is needed **in every mode** (dry-run included).

---

## Before you start

1. `GMAIL_LABEL_ID` is **not yet in the live settings** — add it to `.env`
   yourself (the label the pipeline syncs; the labeler only scans inside it).
   The email connector will not poll without it either.
2. `GMAIL_LABELER_ACCOUNT_EMAIL=you@gmail.com` in `.env` — your personal
   account. Without it the labeler refuses `--auth` and `--apply` outright.
3. The regular email pipeline works (`brain connectors list` shows `email`
   enabled with a recent **last ok**) — the labeler reuses its keychain
   token, its pacing and its client-secret path.
4. Confirmed kid names exist in memory (the labeler reads them from confirmed
   items at runtime; nothing is hardcoded).

All commands below run **from the app root** (`/Users/yos.i/git/Second-brain`)
so that `.env` resolves to the **live** store.

---

## 1. Write the rules file

The rules file is a small JSON mapping a label name to sender **domains** and
**List-Id** patterns. Create it **outside the repo** (never commit personal
domains), for example `~/.config/second-brain/gmail-labeler-rules.json`:

```json
{
  "School": {
    "domains": ["school.example", "teachers-school.example"],
    "list_ids": ["school-announcements.school.example"]
  },
  "Logistics": {
    "domains": ["delivery.example"],
    "list_ids": []
  },
  "Bills": {
    "domains": ["utility.example"],
    "list_ids": []
  },
  "Noise": {
    "domains": ["marketing.example"],
    "list_ids": []
  }
}
```

Notes:

- `domains` match the **sender's domain** (case-insensitive); `list_ids`
  match the `List-Id` header. Both are optional per label; **empty patterns
  are dropped** (an empty string would match every message).
- Only the label names the code knows are honored
  (`School` / `Logistics` / `Bills` / `Noise` — `Kids` comes from memory, and
  anything unmatched lands `SB/Unsorted`). Unknown keys in the file are
  ignored silently.
- An empty file or an unset path is fine too: then everything that is not a
  kid subject lands `SB/Unsorted`, and you can still dry-run to see the flow.

Point the app at the file in `.env` (the config key is
`gmail_labeler_rules_path`):

```bash
echo 'GMAIL_LABELER_RULES_PATH=/Users/YOU/.config/second-brain/gmail-labeler-rules.json' >> .env
```

---

## 2. Dry-run (the default — zero writes)

`BRAIN_ALLOW_LIVE=1` is needed in EVERY mode; export it once per shell:

```bash
cd /Users/yos.i/git/Second-brain
export BRAIN_ALLOW_LIVE=1            # every gmail-label mode needs this
export QDRANT_COLLECTION=brain_items # the LIVE collection — never a *_test one
unset DATABASE_URL                   # let .env provide the live DB
brain gmail-label
```

Do **not** set a test `DATABASE_URL` here — the command refuses non-live
stores unless `BRAIN_ALLOW_LIVE=1`, and with that flag set a wrong store
variable would silently run against the wrong data.

EXAMPLE output (ids, hashes and counts are illustrative only):

```
gmail-label: plan 9f2c1ab77e30d4c8 (12 message(s))
gmail-label: 12 message(s) to label (kids: 2, scan cap 100)
  18c9f2a7e4b6d011 -> SB/School (rules)
  18c9f1b2a3d4e5f6 -> SB/Kids (kid)
  ...
gmail-label: dry-run — zero writes (pass --apply [--confirm-plan <plan hash>] , capped at --max-apply 20, ceiling 50)
```

How to read it:

- The first line (`plan <hash>`) is the plan's content hash. Copy it if you
  want `--apply --confirm-plan <hash>` to write exactly this preview.
- Each message line is its Gmail id, the label it would get, and how it was
  chosen: `kid` (a confirmed kid name as the subject), `rules` (a domain or
  List-Id rule), or `unsorted`.
- Nothing has been written. Expect the scan to take a few minutes (pacing).
- Re-run any time; messages already carrying an SB/ label are skipped.

If you see instead: `gmail-label: the keychain token does not grant
gmail.modify — run 'brain gmail-label --auth' …` — that is the **apply-time
consent gate**; the dry-run itself works on a readonly token, and you only
need step 3 when you are ready to write.

---

## 3. Consent for label writes (`brain gmail-label --auth`)

`--apply` and `--undo` write labels, which needs the `gmail.modify` scope.
Grant it once:

```bash
brain gmail-label --auth
```

- A browser window opens and Google asks you to allow access with
  **two scopes**: *read your mailbox (readonly)* and *manage labels and
  messages (modify)*.
- **Consent with the account in `GMAIL_LABELER_ACCOUNT_EMAIL`.** Before
  anything is stored, the labeler calls `getProfile` and refuses to store the
  token if the consented mailbox differs (e.g. you accidentally consented
  with a work account): nothing is written to the keychain, the mail sync
  keeps its old token, and the message says which mailbox consented.
- This **replaces the refresh token** stored in the macOS login keychain
  (service `second-brain-gmail`) — the same one the email sync uses. The new
  grant is a **superset** (readonly + modify), so the readonly sync keeps
  working unchanged; the sync path itself still requests readonly only.
- The browser flow is interactive; if it fails or is closed, nothing is
  stored — re-run the command.

**Check the sync still works afterwards.** Run a read-only sync pass and then
look at the connector's health line:

```bash
brain connectors sync email --dry-run   # reads only, writes nothing
brain connectors list
```

EXAMPLE (format only):

```
name      enabled  registered  last ok              cursor  quarantined  last error
email     yes      yes         2026-10-26 09:12     yes     0
```

The `email` row should show a fresh **last ok** timestamp and no last error.
The launchd poller keeps running on its own; it uses the same (upgraded)
token transparently.

---

## 4. `--apply` (capped), audit, undo

Preview first (step 2), then apply — either the fresh plan, or exactly the
preview you checked:

```bash
brain gmail-label --apply                          # fresh plan, still capped
brain gmail-label --apply --confirm-plan 9f2c1ab77e30d4c8   # exactly the preview
```

EXAMPLE output:

```
gmail-label: plan 9f2c1ab77e30d4c8 (12 message(s))
gmail-label: 12 message(s) to label (kids: 2, scan cap 100)
  18c9f2a7e4b6d011 -> SB/School (rules)
  ...
gmail-label: batch 7f3c9a2b1d4e — applying up to 20 label(s)
gmail-label: applied 12 label(s), batch 7f3c9a2b1d4e
              (undo: brain gmail-label --undo 7f3c9a2b1d4e)
```

Notes:

- The **batch id is printed BEFORE the first label is written** — the undo
  handle exists even if the run dies mid-apply.
- `--confirm-plan` compares the hash; if the plan changed since the preview
  (new mail arrived, rules/kid names changed) it **refuses** with both hashes
  and exits 78 — re-run the dry-run and confirm the new hash.
- If the audit ledger cannot be written (e.g. its directory is missing and
  not creatable), the run **fails before the first label** — no label is ever
  applied un-undoable.

Look at what a run did (one batch, or every batch):

```bash
brain gmail-label --audit 7f3c9a2b1d4e   # one batch
brain gmail-label --audit                # every batch, in order
```

EXAMPLE (format only):

```
  [7f3c9a2b1d4e] apply: 18c9f2a7e4b6d011 -> SB/School
  [7f3c9a2b1d4e] apply: 18c9f1b2a3d4e5f6 -> SB/Kids
```

The audit reads the JSONL ledger (never the prunable ops table), so entries
stay available indefinitely. A failed apply/undo shows as
`apply FAILED: …` / `undo FAILED: …` with the error.

Undo one batch — it removes **exactly the labels that batch added** (your own
labels on those messages, and labels added by other batches, are untouched):

```bash
brain gmail-label --undo 7f3c9a2b1d4e
```

EXAMPLE output:

```
gmail-label: undo batch 7f3c9a2b1d4e: undone 11, already-undone 0, failed 1 of 12
```

- The undo **continues past per-item errors** (a message deleted since the
  apply, a label no longer present): each failure is counted and recorded in
  the ledger, and the rest of the batch still proceeds. The exit code stays 0
  — the counts are the report.
- **Re-running an undo is safe**: items (and whole batches) already undone
  are skipped and reported as `already-undone`, never churned.
- An unknown/typo'd batch id reports `no applied labels found for batch …`
  instead of a silent `undone 0`.

Everyday rhythm: dry-run → read the plan → `--apply` (each run caps at
`--max-apply`, default 20, minimum 1, ceiling 50) → `--audit` → keep the
batch ids until you are happy.

---

## 5. Rollback

Two levels, depending on how far you want to go.

**Undo the labels.** List every batch (`--audit` with no argument), then undo
each batch id in turn — the last-applied first is simplest. Re-running an
undo is safe (already-undone batches are skipped and reported), so scripting
the whole list in one line is fine:

```bash
brain gmail-label --audit
brain gmail-label --undo <batch-3>
brain gmail-label --undo <batch-2>
brain gmail-label --undo <batch-1>
```

After each undo, `gmail-label --audit <batch>` still shows what the batch
*did* (the ledger is history, not state — and it is the durable JSONL file,
not the pruned ops table, so old batches stay undoable); re-running
`gmail-label` (dry-run) should now re-plan those messages.

**Revoke the modify grant.** This fully closes the write path:

1. Open <https://myaccount.google.com/permissions> (Google Account →
   Security → *Third-party apps with account access*).
2. Remove access for this app. That revokes the **whole** refresh token —
   including the readonly part — so the email sync stops too.
3. Restore the sync with the ordinary readonly consent:
   `brain gmail-auth --client-secret /path/to/secret.json`
   (the client secret stays wherever you keep it, outside the repo).
4. If you still want the labeler, re-run its own consent
   (`brain gmail-label --auth`, step 3) — it re-verifies the mailbox against
   `GMAIL_LABELER_ACCOUNT_EMAIL` before storing.
5. Verify again with `brain connectors list` (fresh **last ok**, no error).

---

## Troubleshooting

- **`GMAIL_LABEL_ID unset` / the plan is empty but mail exists** —
  `GMAIL_LABEL_ID` is **not yet in the live settings**; add it to `.env`
  (plus `GMAIL_QUERY` if you use one). The scan scope is the connector's
  label + query, and `--max-scan` (default 100) bounds each run — raise it
  for one catch-up run if needed.
- **`GMAIL_LABELER_ACCOUNT_EMAIL is not set — refusing`** — add your Gmail
  address to `.env` as `GMAIL_LABELER_ACCOUNT_EMAIL=you@gmail.com`. `--auth`
  and `--apply` refuse without it (nothing is consented, stored or written).
- **`refusing to store the token — you consented as '…', but
  GMAIL_LABELER_ACCOUNT_EMAIL is '…'`** — the consent went to a DIFFERENT
  Google account. Nothing was stored; consent again in the browser with the
  pinned address (or fix the setting if it is wrong).
- **`refusing to apply — the keychain token's mailbox is '…'`** — the
  keychain token now points at a different mailbox than the pin (e.g. someone
  re-consented elsewhere). Re-run `brain gmail-label --auth` with the pinned
  account.
- **`client secret missing/unreadable` / `not the Google OAuth endpoint`** —
  `GMAIL_CREDENTIALS_PATH` does not point at a valid OAuth client JSON; the
  labeler refuses a secret file whose `token_uri` is not Google's token
  endpoint (the refresh token is POSTed there, so it must never leave
  Google).
- **`the audit ledger is unwritable at …`** — the JSONL ledger
  (`~/.second-brain-files/gmail-label-audit.jsonl`, or
  `GMAIL_LABELER_AUDIT_PATH`) could not be written; the run failed BEFORE any
  label was applied. Fix the path/permissions and re-run.
- **`gmail.modify is not consented — run 'brain gmail-label --auth'`** — the
  write-time 403 backstop; grant consent, then re-run. No messages were
  touched (the gate fires before any write).
- **`could not verify the token's granted scopes`** — the token was revoked
  or expired in Google's account console; run `brain gmail-label --auth`
  again (or `brain gmail-auth` to go back to readonly-only for the SYNC).
- **`--max-apply must be >= 1`** — the cap cannot be removed; the old
  `-1 = unlimited` behavior is gone, and the hard ceiling is 50 per run.
