"""Matching + validation behavior of the extraction eval engine (synthetic rows)."""

from zoneinfo import ZoneInfo

import pytest

from brain.services.eval_extraction import EvalError, evaluate, format_aggregate


def row(**over):
    base = {
        "email_id": "e1",
        "child_id": "g1",
        "type": "event",
        "title": "alpha beta gamma",
        "start": "2026-10-19T09:00:00",
        "all_day": False,
        "applies_to": "rona",
    }
    base.update(over)
    return base


def agg(gold, pred, **kw):
    return format_aggregate(evaluate(gold, pred, **kw))


def test_perfect_match_scores_one():
    out = agg([row()], [row(child_id="p1")])
    assert "precision: 1.0" in out
    assert "recall: 1.0" in out
    assert "f1: 1.0" in out
    assert "matched: 1" in out
    assert "date_exact_match_rate: 1.0" in out
    assert "applies_to_accuracy: 1.0" in out


def test_time_within_tolerance_matches_but_not_exact():
    out = agg([row()], [row(child_id="p1", start="2026-10-19T09:40:00")])
    assert "matched: 1" in out
    assert "date_exact_match_rate: 0.0" in out


def test_time_beyond_tolerance_is_fp_fn():
    out = agg([row()], [row(child_id="p1", start="2026-10-19T11:00:00")])
    assert "matched: 0" in out
    assert "precision: 0.0" in out
    assert "recall: 0.0" in out


def test_type_mismatch_never_matches():
    out = agg([row()], [row(child_id="p1", type="task")])
    assert "matched: 0" in out


def test_known_applies_to_mismatch_blocks_match():
    out = agg([row()], [row(child_id="p1", applies_to="itamar")])
    assert "matched: 0" in out


def test_me_vs_nobody_blocks_match():
    out = agg([row(applies_to="me")], [row(child_id="p1", applies_to="nobody")])
    assert "matched: 0" in out


def test_unknown_applies_to_is_a_wildcard_but_scored_wrong():
    out = agg([row(applies_to="unknown")], [row(child_id="p1", applies_to="rona")])
    assert "matched: 1" in out
    assert "applies_to_accuracy: 0.0" in out
    assert "applies_to_pred_unknown: 0" in out


def test_applies_to_json_list_from_gold_schema():
    # worker_3's schema uses a JSON LIST for applies_to, e.g. ["Rona"]
    out = agg([row(applies_to=["Rona"])], [row(child_id="p1", applies_to="Itamar")])
    assert "matched: 0" in out
    out = agg(
        [row(applies_to=["Rona", "me"])],
        [row(child_id="p1", applies_to="me, rona")],
    )
    assert "matched: 1" in out
    assert "applies_to_accuracy: 1.0" in out


def test_unexpected_field_types_raise_not_coerced():
    with pytest.raises(EvalError, match="applies_to"):
        evaluate([row(applies_to=5)], [])
    with pytest.raises(EvalError, match="relation"):
        evaluate([row(relation=5)], [])
    with pytest.raises(EvalError, match="importance"):
        evaluate([row(importance=3.5)], [])
    with pytest.raises(EvalError, match="all_day"):
        evaluate([row(all_day=1)], [])
    with pytest.raises(EvalError, match="notes"):
        evaluate([row(notes=["x"])], [])
    with pytest.raises(EvalError, match="email_text"):
        evaluate([row(email_text=7)], [])
    with pytest.raises(EvalError, match="source_quote"):
        evaluate([], [row(child_id="p1", source_quote=9)])
    with pytest.raises(EvalError, match="actionability"):
        evaluate([row(actionability=3)], [])
    with pytest.raises(EvalError, match="date_conflict"):
        evaluate([row(date_conflict=1)], [])


def test_pred_unknown_against_known_gold_counts_wrong_and_reported():
    out = agg([row()], [row(child_id="p1", applies_to="unknown")])
    assert "matched: 1" in out
    assert "applies_to_accuracy: 0.0" in out
    assert "applies_to_pred_unknown: 1" in out
    assert "applies_to_pred_unknown_rate: 1.0" in out


def test_title_below_threshold_never_matches():
    out = agg([row()], [row(child_id="p1", title="qzx jvkw plmnb")])
    assert "matched: 0" in out


def test_title_threshold_is_configurable():
    out = agg(
        [row(title="white shirt day")],
        [row(child_id="p1", title="white shirt morning")],
        title_threshold=0.95,
    )
    assert "matched: 0" in out
    out = agg(
        [row(title="white shirt day")],
        [row(child_id="p1", title="white shirt morning")],
        title_threshold=0.3,
    )
    assert "matched: 1" in out


def test_greedy_pairs_two_same_day_events():
    gold = [
        row(child_id="g1", title="alpha beta gamma"),
        row(child_id="g2", title="delta epsilon zeta"),
    ]
    pred = [
        row(child_id="p1", title="delta epsilon zeta"),
        row(child_id="p2", title="alpha beta gamma"),
    ]
    out = agg(gold, pred)
    assert "matched: 2" in out
    assert "unmatched_gold: 0" in out
    assert "unmatched_pred: 0" in out


def test_single_pred_leaves_other_event_unmatched():
    gold = [
        row(child_id="g1", title="alpha beta gamma"),
        row(child_id="g2", title="delta epsilon zeta"),
    ]
    pred = [row(child_id="p1", title="delta epsilon zeta")]
    out = agg(gold, pred)
    assert "matched: 1" in out
    assert "unmatched_gold: 1" in out
    assert "recall: 0.5" in out
    assert "precision: 1.0" in out


def test_dateless_children_match_on_title():
    out = agg(
        [row(type="fact", title="reading book daily", start=None)],
        [row(child_id="p1", type="fact", title="reading book daily", start=None)],
    )
    assert "matched: 1" in out
    # no comparable dates on a dateless pair -> rate undefined
    assert "date_exact_match_rate: n/a" in out


def test_dateless_pred_cannot_match_dated_gold():
    out = agg(
        [row()],
        [row(child_id="p1", title="alpha beta gamma", start=None)],
    )
    assert "matched: 0" in out


def test_all_day_gold_can_match_timed_pred_inexactly():
    out = agg(
        [row(all_day=True, start="2026-10-19")],
        [row(child_id="p1", all_day=False, start="2026-10-19T08:00:00")],
    )
    assert "matched: 1" in out
    assert "date_exact_match_rate: 0.0" in out


def test_task_matched_on_due_when_no_start():
    gold = row(type="task", title="volunteer reply", start=None, due="2026-11-01")
    gold["all_day"] = True
    out = agg(
        [gold],
        [
            row(
                child_id="p1",
                type="task",
                title="volunteer reply",
                start=None,
                due="2026-11-01T09:00:00",
            )
        ],
    )
    assert "matched: 1" in out
    assert "date_exact_match_rate: 0.0" in out


def test_date_only_due_implies_all_day():
    gold = row(type="task", title="volunteer reply", start=None, due="2026-11-01")
    del gold["all_day"]  # no explicit flag: the date-only string implies all-day
    pred = row(
        child_id="p1",
        type="task",
        title="volunteer reply",
        start=None,
        due="2026-11-01T09:00:00",
    )
    del pred["all_day"]
    out = agg([gold], [pred])
    assert "matched: 1" in out
    assert "date_exact_match_rate: 0.0" in out


def test_cross_email_rows_are_fp_or_fn():
    out = agg([row(email_id="e1")], [row(child_id="p1", email_id="e2")])
    assert "matched: 0" in out
    assert "unmatched_gold: 1" in out
    assert "unmatched_pred: 1" in out


def test_per_type_metrics():
    gold = [
        row(child_id="g1", title="alpha"),
        row(child_id="g2", title="beta", start="2026-10-19T10:00:00"),
        row(child_id="g3", type="task", title="gamma", start=None, due="2026-11-01"),
    ]
    pred = [
        row(child_id="p1", title="alpha"),
        row(child_id="p2", type="task", title="gamma", start=None, due="2026-11-01"),
        row(child_id="p3", type="note", title="extra", start=None),
    ]
    out = agg(gold, pred)
    assert "matched: 2" in out
    assert "type_event_precision: 1.0" in out
    assert "type_event_recall: 0.5" in out
    assert "type_task_precision: 1.0" in out
    assert "type_task_recall: 1.0" in out
    assert "type_note_precision: 0.0" in out
    assert "type_note_recall: n/a" in out


def test_duplicate_child_id_rejected():
    with pytest.raises(EvalError, match="duplicate"):
        evaluate([row(), row()], [])


def test_missing_required_field_rejected():
    bad = row()
    del bad["title"]
    with pytest.raises(EvalError, match="title"):
        evaluate([bad], [])


def test_bad_datetime_rejected():
    with pytest.raises(EvalError, match="datetime"):
        evaluate([row(start="not-a-date")], [])


def test_aware_datetime_converted_to_local_wall_clock():
    # user decision: CONVERT, don't reject — offset-bearing values are
    # converted to the configured local zone and made naive
    jerusalem = ZoneInfo("Asia/Jerusalem")
    # (a) summer: UTC+3 — 06:00Z == 09:00 local
    out = agg(
        [row(start="2026-10-19T09:00:00")],
        [row(child_id="p1", start="2026-10-19T06:00:00+00:00")],
        tz=jerusalem,
    )
    assert "matched: 1" in out
    assert "date_exact_match_rate: 1.0" in out
    # (b) after Israel leaves summer time (Sun 25 Oct 2026): UTC+2 — proves
    # the conversion is real ZoneInfo, not a fixed offset
    out = agg(
        [row(start="2026-10-28T15:00:00")],
        [row(child_id="p1", start="2026-10-28T13:00:00+00:00")],
        tz=jerusalem,
    )
    assert "matched: 1" in out
    assert "date_exact_match_rate: 1.0" in out
    # (c) mixed aware pred / naive gold must not raise TypeError — with the
    # DEFAULT tz (UTC, no explicit zone) the aware value folds to 06:00
    # wall-clock vs the naive 09:00 gold: 3h apart, over the ±1h tolerance,
    # so no match (and no crash)
    out = agg(
        [row(start="2026-10-19T09:00:00")],
        [row(child_id="p1", start="2026-10-19T06:00:00+00:00")],
    )
    assert "matched: 0" in out


def test_all_day_inferred_from_effective_field_only():
    # start (timed, present) is the effective date: the date-only due must NOT
    # make this child all-day
    gold = row(start="2026-10-19T09:00:00", due="2026-11-01")
    del gold["all_day"]
    pred = row(child_id="p1", start="2026-10-19T09:00:00")
    del pred["all_day"]
    out = agg([gold], [pred])
    assert "matched: 1" in out
    assert "date_exact_match_rate: 1.0" in out  # both timed, same minute


def test_applies_to_accuracy_excl_gold_unknown_is_reported_and_correct():
    """Manager decision (23 Oct): applies_to is reported TWICE — raw, and
    excluding gold {unknown} rows (they only reward guessing)."""
    gold = [
        row(child_id="g1"),  # ok, gold-specific
        row(child_id="g2", applies_to="unknown"),  # wildcard match — EXCLUDED
        # dateless match, gold-specific, WRONG:
        row(child_id="g3", applies_to="rona", start=None, due=None),
    ]
    pred = [
        row(child_id="p1"),
        row(child_id="p2", applies_to="itamar"),
        row(child_id="p3", applies_to="me", start=None, due=None),
    ]
    out = agg(gold, pred)
    assert "applies_to_accuracy: 0.3333" in out  # raw, unchanged
    assert "applies_to_accuracy_excl_gold_unknown: 0.5" in out
    assert "applies_to_excl_gold_unknown_denominator: 2" in out


def test_applies_to_excl_gold_unknown_is_n_a_when_no_gold_specific_match():
    out = agg([row(applies_to="unknown")], [row(child_id="p1", applies_to="rona")])
    assert "applies_to_accuracy_excl_gold_unknown: n/a" in out
    assert "applies_to_excl_gold_unknown_denominator: 0" in out


def test_existing_aggregate_fields_are_byte_identical_on_a_mixed_fixture():
    """The new metric must ADD lines, never alter an existing field:value."""
    gold = [
        row(child_id="g1"),
        row(child_id="g2", applies_to="unknown"),
        row(child_id="g3", applies_to="rona", start=None, due=None),
        row(child_id="g4", title="unrelated delta epsilon"),
    ]
    pred = [
        row(child_id="p1"),
        row(child_id="p2", applies_to="itamar", title="alpha beta gamma"),
        row(child_id="p3", applies_to="me", start=None, due=None),
        row(child_id="p4", title="alpha beta gamma"),
    ]
    expected = [
        "gold_children: 4",
        "pred_children: 4",
        "matched: 3",
        "unmatched_gold: 1",
        "unmatched_pred: 1",
        "precision: 0.75",
        "recall: 0.75",
        "f1: 0.75",
        "type_event_gold: 4",
        "type_event_pred: 4",
        "type_event_matched: 3",
        "type_event_precision: 0.75",
        "type_event_recall: 0.75",
        "type_event_f1: 0.75",
        "date_exact_match_rate: 1.0",
        "applies_to_accuracy: 0.3333",
        "applies_to_pred_unknown: 0",
        "applies_to_pred_unknown_rate: 0.0",
        "grounding_rate: n/a",
        "grounding_grounded: 0",
        "grounding_denominator: 0",
        "grounding_unverifiable: 4",
        "grounding_derived: 0",
        "date_conflict_gold: 0",
        "date_conflict_reproduced: 0",
        "date_conflict_recall: n/a",
    ]
    fields = [line for line in agg(gold, pred).splitlines()]
    new_fields = {
        "applies_to_accuracy_excl_gold_unknown",
        "applies_to_excl_gold_unknown_denominator",
    }
    # every OLD field keeps its exact position and value
    old_in_output = [line for line in fields if line.split(":")[0] not in new_fields]
    assert old_in_output == expected
    # the new fields sit inside the applies block, nothing else moved
    idx = fields.index("applies_to_pred_unknown_rate: 0.0")
    assert fields[idx + 1 : idx + 3] == [
        "applies_to_accuracy_excl_gold_unknown: 0.5",
        "applies_to_excl_gold_unknown_denominator: 2",
    ]
