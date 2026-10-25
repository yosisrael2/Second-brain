# Gmail SB/ Labeler — Runbook (v1)

This runbook lets you run the SB/ labeler **alone, step by step**. It labels
mail in Gmail by **deterministic rules over headers only** — no Jev, no
external LLM, no message text leaves your machine. It is **label-only**: it
adds `SB/…` labels and never removes INBOX, never archives, trashes or
deletes anything, and it creates **no Gmail filters**.

Safety rails you are relying on:

- **Dry-run is the default.** Without `--apply` the command performs zero
  write calls (list + metadata GETs only) and prints what it *would* do.
- `--apply` is explicit and **capped at 20 labels per run** (`--max-apply`).
- Every applied label is written to the ops ledger (`ops_events`, component
  `gmail-label`) so any run is auditable and **undo-able batch by batch**.
- A message is labeled **once** (already-`SB/`-labeled messages are skipped)
  and gets at most one label. Precedence: Kids > School > Logistics > Bills >
  Noise > Unsorted.
- Message selection uses **the same label + query the email connector syncs**
  (`GMAIL_LABEL_ID` / `GMAIL_QUERY`), so the labeler cannot reach outside the
  pipeline's scope.
- Kid matching is **subject-only, full-token** on confirmed kid names from
  memory — a classmate's name or a substring never triggers the Kids label.

---

## Before you start

1. The regular email pipeline works (`brain connectors list` shows `email`
   enabled with a recent **last ok**) — the labeler reuses its scope, its
   pacing and the keychain token.
2. Your app is up to date (this feature is on `main` as of
   `575e2a21a504b0a9e3ae86409b2845a400ff7b40`, 2026-10-26).
3. `GMAIL_LABEL_ID` is set in `.env` (the label the pipeline syncs — the
   labeler only scans inside it).
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
  match the `List-Id` header. Both are optional per label.
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

Run from the app root, with the **live-store opt-in** the live run needs:

```bash
cd /Users/yos.i/git/Second-brain
export BRAIN_ALLOW_LIVE=1            # the guard refuses the LIVE store without it
export QDRANT_COLLECTION=brain_items # the LIVE collection — never a *_test one
unset DATABASE_URL                   # let .env provide the live DB
brain gmail-label
```

Do **not** set a test `DATABASE_URL` here — the command refuses non-live
stores unless `BRAIN_ALLOW_LIVE=1`, and with that flag set a wrong store
variable would silently run against the wrong data.

EXAMPLE output (ids and counts are illustrative only):

```
gmail-label: 12 message(s) to label (kids: 2, scan cap 100)
  18c9f2a7e4b6d011 -> SB/School (rules)
  18c9f1b2a3d4e5f6 -> SB/Kids (kid)
  ...
gmail-label: dry-run — zero writes (pass --apply, capped at --max-apply 20)
```

How to read it:

- The first line is the plan size: how many messages in the sync scope are
  not yet `SB/`-labeled, how many confirmed kid names were loaded, and the
  scan bound.
- Each following line is one message: its Gmail id, the label it would get,
  and how it was chosen: `kid` (a confirmed kid name as the subject),
  `rules` (a domain or List-Id rule), or `unsorted`.
- Nothing has been written. Re-run any time; the dry-run also paces its
  metadata GETs, so a large scan takes a few minutes.

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

## 4. First `--apply` (cap 20), audit, undo

Apply, capped at the default 20:

```bash
brain gmail-label --apply
```

EXAMPLE output:

```
gmail-label: 12 message(s) to label (kids: 2, scan cap 100)
  18c9f2a7e4b6d011 -> SB/School (rules)
  ...
gmail-label: applied 12 label(s), batch 7f3c9a2b1d4e
              (undo: brain gmail-label --undo 7f3c9a2b1d4e)
```

Note the **batch id** in the output — that is your undo handle. Every run
gets its own batch, so a mistake is bounded to one run's worth of labels.

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

Undo one batch — it removes **exactly the labels that batch added** (your own
labels on those messages, and labels added by other batches, are untouched):

```bash
brain gmail-label --undo 7f3c9a2b1d4e
```

EXAMPLE output: `gmail-label: undone 12 label(s) for batch 7f3c9a2b1d4e`

Everyday rhythm: dry-run → read the plan → `--apply` (each run caps at 20) →
`--audit` → keep the batch ids until you are happy.

---

## 5. Rollback

Two levels, depending on how far you want to go.

**Undo the labels.** List every batch (`--audit` with no argument), then undo
each batch id in turn — the last-applied first is simplest:

```bash
brain gmail-label --audit
brain gmail-label --undo <batch-3>
brain gmail-label --undo <batch-2>
brain gmail-label --undo <batch-1>
```

After each undo, `gmail-label --audit <batch>` still shows what the batch
*did* (the ledger is history, not state); re-running `gmail-label` (dry-run)
should now re-plan those messages.

**Revoke the modify grant.** This fully closes the write path:

1. Open <https://myaccount.google.com/permissions> (Google Account →
   Security → *Third-party apps with account access*).
2. Remove access for this app. That revokes the **whole** refresh token —
   including the readonly part — so the email sync stops too.
3. Restore the sync with the ordinary readonly consent:
   `brain gmail-auth --client-secret /path/to/secret.json`
   (the client secret stays wherever you keep it, outside the repo).
4. Verify again with `brain connectors list` (fresh **last ok**, no error).

---

## Troubleshooting

- **`client secret missing/unreadable`** — `GMAIL_CREDENTIALS_PATH` does not
  point at the OAuth client JSON; the labeler needs it to verify the token's
  scopes.
- **`no Google refresh token in the keychain`** — run
  `brain gmail-label --auth` (step 3).
- **`gmail.modify is not consented — run 'brain gmail-label --auth'`** — the
  write-time 403 backstop; grant consent, then re-run. No messages were
  touched (the gate fires before any write).
- **`could not verify the token's granted scopes`** — the token was revoked
  or expired in Google's account console; run `brain gmail-label --auth`
  again (or `brain gmail-auth` to go back to readonly-only).
- **The plan is empty but mail exists** — the scan scope is the connector's
  label + query (`GMAIL_LABEL_ID` / `GMAIL_QUERY`); check that those still
  match where new mail lands, and that `--max-scan` (default 100) covers the
  backlog — raise it for one catch-up run if needed.
- **Exit code 78 with "nothing was read or written"** — the consent gate:
  expected on a readonly-only token with `--apply`/`--undo`; see step 3.
