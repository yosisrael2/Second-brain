# A2 held-out eval — labelling + scoring protocol (next 3 weeks of school email)

Approved by the manager (thread eval/heldout) with two overrides baked in:
labeller+scorer is one role (worker_3), and artifacts live OUTSIDE /private/tmp.
Corpus window: the 3 weeks starting 23 Oct 2026 (freeze on/after 13 Nov 2026).
This file is committed to the repo in the #39 follow-up PR, not before.

Purpose: one untouched corpus, labelled once, scored exactly once. The dev set
(`school_emails.txt`, gold sha `693a2ab0…`) stays the tuning surface with its
6-run budget; this protocol produces the one number that says how the extractor
does on mail nobody tuned against.

## Roles (separation of knowledge)

- **LABELLER + SCORER** — worker_3 (same role, as with the dev gold). Labels
  child rows against raw emails, then scores. The separation that matters is
  from the predictor. Before the freeze the manager reviews ALL 'unsure' rows
  plus a 25% random sample of 'certain' rows (seeded RNG, seed recorded in the
  freeze note); labelling decisions change only before the freeze, never after.
- **PREDICTOR** — worker_2. Runs the extraction pipeline. Never reads
  `~/.second-brain-evals/` at all.
- **Manager** — approves the freeze; receives aggregate-only numbers.

## Artifacts location

`~/.second-brain-evals/a2-heldout/` (outside the repo, mode 700 — /private/tmp
is wiped on reboot and too flimsy for a 3-week corpus). Files:
`a2_heldout_emails.txt`, `a2_heldout_gold_FROZEN_<sha8>.jsonl`, and a freeze
note per round.

## Step 1 — corpus freeze (on/after 13 Nov 2026)

1. Export the raw school emails received in the window (23 Oct – 12 Nov 2026)
   to `~/.second-brain-evals/a2-heldout/`, same `=====` delimited format as
   `school_emails.txt`, as `a2_heldout_emails.txt`.
2. `shasum -a 256` the file; the hash goes into the freeze note. No edits
   after this point — a corrected corpus means a NEW freeze and a voided round.

## Step 2 — gold labelling (LABELLER, manager-reviewed)

1. Label child rows against the frozen raw text only — same schema as
   `school_gold.jsonl`, same field rules as the dev gold: `source_quote` an
   exact substring of its email, dates as ISO local wall-clock (no UTC
   offsets), `applies_to` a JSON list, relations only legal kinds, weekday/
   date consistency code-checked, Hebrew-calendar anchors as in
   `verify_gold.py`.
2. Run the dev validators (`build_gold.py` checks + `verify_gold.py`) until
   clean.
3. Manager review gate: ALL rows the labeller marked 'unsure' + a 25% random
   sample of 'certain' rows (seeded RNG, seed recorded). Disagreements are
   fixed in the labelling file BEFORE the freeze — never after.
4. Freeze: `a2_heldout_gold_FROZEN_<sha8>.jsonl` + a freeze note recording
   sha256, row count, per-email child counts, the sample seed, and the
   manager-review outcome. Publish ONLY the sha to predictor and manager.
5. After the freeze the file is read-only for everyone. The scorer re-verifies
   the sha before every scoring command and reports the verification.

## Step 3 — exactly one prediction run (PREDICTOR)

1. Run the full pipeline over the frozen corpus — same code path as
   production (grounding, dates, applies_to, relations), the prompt version
   being retired-or-promoted, in mailbox order.
2. Deliver one pred JSONL (same schema) + the pipeline commit sha to the
   scorer. A crashed run that produced no scored output may be restarted; a
   scored run is final.

## Step 4 — exactly one scoring (SCORER)

1. Verify the gold sha and record the pred's pipeline sha.
2. `brain eval-extraction --gold <frozen> --pred <pred> --aggregate-only`
   (title threshold and timezone exactly as the dev runs: 0.3, Asia/Jerusalem).
3. Send the aggregate block (metrics below) to predictor + manager.
4. No per-child diagnostics for the held-out round — there is nothing left to
   tune, and every leak narrows the next held-out.
5. No re-scoring, period. If a harness bug invalidates the score, the ROUND is
   voided: fix the bug, re-freeze if the corpus changed, restart from Step 2,
   and say so in the freeze note. Both the voiding and the restart are
   reported to the manager before anything is re-run.

## Metrics — identical block to the dev runs

gold/pred children, matched, precision/recall/F1 overall and per kind
(event/task/reminder/fact), `date_exact_match_rate`, `applies_to` accuracy +
pred-unknown rate, grounding rate (grounded/unverifiable/derived), relation
reproduction per kind, `date_conflict` recall. The held-out aggregate is
compared to the LAST dev run only to state the expected size of the gap —
never to iterate.

### Eval conventions (manager, decided 23 Oct 2026)

1. **applies_to is reported TWICE** — raw, and excluding gold `{unknown}`
   rows (unknown-gold pairs say nothing about audience accuracy, they only
   reward guessing). Used from run #5 on and for the held-out.
2. **Held-out labelling follows R1**: when an email comes from a known class
   teacher and nothing in the text narrows the audience, label that teacher's
   kid rather than `unknown`. The dev gold stays frozen as it is — no
   relabelling, so dev vs held-out numbers are not directly comparable on
   this metric.

## Anti-leak rules (all roles, all rounds)

- Gold text never appears in any report: aggregates and token-only counts.
- Prompt or pipeline changes may never be justified by held-out numbers —
  there is only ever one number.
- The dev-set diagnostics rules (no titles/quotes/ids to the predictor) carry
  over unchanged.
