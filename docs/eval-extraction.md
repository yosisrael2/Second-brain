# Extraction eval harness

Scores meaning-extraction output against the hand-labeled gold set (design:
`meaning-extraction-spec.md` + the v2/v3/v4 revisions, items 25-31). The gold
labels are drafted by a worker who builds no extraction, reviewed by the
manager, and frozen; the extraction developer never reads the gold files —
they consume the aggregate metrics this harness prints.

```bash
brain eval-extraction --gold gold.jsonl --pred pred.jsonl --aggregate-only
brain eval-extraction --gold gold.jsonl --pred pred.jsonl --report out/report.md
brain eval-extraction --gold gold.jsonl --pred pred.jsonl --timezone Asia/Jerusalem
```

Exit 0 on a successful run (the scores themselves are never an exit
condition); exit 1 with a clear message on unreadable inputs, schema
violations (invalid JSON line, missing required field, duplicate child_id,
unparseable date) or an invalid --timezone.

## Input format (JSONL)

One JSON object per child; both files use the same schema. Unknown fields are
ignored, so the labeling schema can grow without breaking the harness.

| Field           | Required | Notes                                                                 |
| --------------- | -------- | --------------------------------------------------------------------- |
| `email_id`      | yes      | groups children per source email; matching never crosses emails        |
| `child_id`      | yes      | unique within one file; relation targets refer to these                |
| `type`          | yes      | free string (`event`, `task`, `reminder`, `fact`, ...); compared case-insensitively |
| `title`         | yes      | source language (Hebrew or English)                                   |
| `start`         | no       | ISO local wall-clock (`2026-10-19T13:35:00`); offset-bearing values are converted to the local zone (see Timezones); a date-only effective date implies all-day |
| `end`           | no       | carried, not scored                                                    |
| `all_day`       | no       | explicit flag; a date-only EFFECTIVE date (`start` if present, else `due`) implies true |
| `due`           | no       | tasks: used as the matching date when `start` is absent                |
| `applies_to`    | no       | JSON string (`"rona"`, `"rona, itamar"`, `"me"`, `"nobody"`, `"unknown"`, empty) or JSON list of strings (`["Rona", "me"]`) — both normalize to the same token set; any other type is an error |
| `importance`    | no       | carried, not scored                                                    |
| `actionability` | no       | carried, not scored                                                    |
| `source_quote`  | no       | verbatim text from the source email (verified for preds)               |
| `relation`      | no       | `{"kind": "supersedes", "target": "<child_id>"}`, or `"supersedes:<child_id>"` |
| `date_conflict` | no       | boolean — the ONLY signal, gold and pred alike (words in `notes` never set it) |
| `notes`         | no       | free text                                                              |
| `email_text`    | no       | gold rows only: the full source text used for grounding checks         |

Email text for the grounding check: the first non-empty `email_text` per
`email_id` among gold rows, overridable per email by a sidecar file
`--emails emails.jsonl` with rows of `{"email_id", "email_text"}`.

## Timezones

The schema is local wall-clock. A `start`/`due` WITH a UTC offset is
converted to the configured local zone and then made naive; a value without
an offset is taken as local wall-clock as written. The zone comes from
`Settings.timezone` (env `TIMEZONE`; `--timezone` overrides; an invalid zone
exits 1). Conversion — never a fixed offset — keeps day and ±1h comparisons
correct across DST changes (e.g. Israel leaves summer time on Sun 25 Oct
2026). `evaluate()` takes the zone as an explicit `tz` keyword so the engine
stays pure.

## Matching (deterministic, documented)

- Per `email_id`; the union of emails is walked in sorted order.
- Hard gates for a candidate pair: same type; same local calendar day of the
  effective date (`start`, else `due`); when both sides are timed, times at
  most ±1 hour apart (all-day rows gate on the day only); `applies_to`
  compatible — normalized token sets equal, where `unknown`/empty is a
  wildcard and two different known values never match (mirroring the dedupe
  rule: grade 2 vs grade 5 the same day stay separate).
- Children with no date at all match only dateless children of the same
  email; a dateless pred can never match a dated gold row.
- Assignment: greedy over all gate-compatible pairs, highest title similarity
  first, ties broken by gold then pred `child_id` — no randomness anywhere.
  Title similarity is `max(SequenceMatcher ratio, token Jaccard)` over
  normalized titles (shared normalizer: NFKC, bidi marks, nikud, geresh/
  gershayim, maqaf and en/em dashes unified to '-', whitespace, case), so it
  behaves identically for Hebrew and
  English. Pairs below `--title-threshold` (default 0.3) never match. Greedy
  is used instead of Hungarian because per-email candidate sets are tiny and
  it avoids a scipy dependency.
- A matched pair is a true positive; an unmatched pred is a false positive;
  an unmatched gold row is a false negative.

## Metrics

Printed as `key: value` lines (fractions rounded to 4 decimals, `n/a` when a
rate has no denominator):

- `precision` / `recall` / `f1` overall (micro) and `type_<t>_*` per type.
- Field types are validated: an `applies_to` that is neither a string nor a
  list of strings, a non-boolean `all_day`/`date_conflict`, a non-integer
  `importance`, or a non-string `relation`/`notes`/`email_text`/
  `source_quote`/`actionability` raises an error instead of being silently
  coerced (a silent fallback to `unknown` would make every row match).
- `date_exact_match_rate`: matched pairs whose compared date is exactly equal
  (same day, same all_day-ness, same minute for timed rows) — matching
  tolerates ±1h, this metric reports how often the prediction was exact.
- `applies_to_accuracy` over matched pairs, plus
  `applies_to_pred_unknown[_rate]` — a pred that answers `unknown` against a
  known gold value is a wrong answer and is reported separately, so a model
  that always says `unknown` cannot score perfectly.
- `grounding_rate`: pred children whose `source_quote` verifies as a substring
  of the email text after normalization (NFKC, bidi/invisible marks stripped,
  nikud stripped, geresh/gershayim unified, maqaf and en/em dashes unified
  to '-', whitespace collapsed, casefolded
  — one shared implementation in `src/brain/services/textnorm.py`). Preds
  whose email text is unknown are counted in `grounding_unverifiable` and
  excluded from the denominator (surfaced, never silently dropped). Derived
  preds (relation kind `derived_from`) with an empty quote are excluded too
  (`grounding_derived`): they cite the facts they derive from and have no
  direct quote (design item 22); a derived pred that does carry a quote is
  verified normally. An empty quote on any other pred is still a failure.
- `relation_<kind>_[gold|reproduced|unresolvable|rate]`: a gold relation
  (kind, target) is reproduced only when both endpoints matched AND the pred
  matched to the source carries the same kind pointing at the pred matched to
  the target (the link may be recorded on either pred child; a correct kind
  with the wrong target does not count). Relations whose gold target is not
  labeled are `unresolvable` and excluded from the rate.
- `date_conflict_[gold|reproduced|recall]`: gold children flagged
  `date_conflict` whose matched pred is flagged too. The explicit boolean
  field is the only signal, gold and pred alike — notes text never sets it.

## Output modes

- `--aggregate-only`: ONLY the numbers — no titles, quotes, or email ids
  (ids would leak which emails are hard). This is what the extraction
  developer (worker_2) sees.
- Full mode: the same aggregates on stdout, plus a per-child markdown report
  (matched pairs with titles/quotes/verification verdicts, missed gold rows,
  false positives, grounding failures) written for the manager. The default
  report path is a timestamped file in the system temp directory (never the
  repo); pass `--report <path>` to choose it.

## Tests

`pytest tests/eval/extraction` — offline unit tests over synthetic fixtures
(`fixtures/gold_tiny.jsonl` / `pred_tiny.jsonl`); the real gold set is never
in the repo tree of the extraction developer's checkout. Normalizer tests
cover the exact cases from the design review: `ז'` vs `ז׳` and `סת"ם` vs
`סת״ם`.
