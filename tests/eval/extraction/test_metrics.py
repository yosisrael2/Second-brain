"""Relation reproduction, date_conflict recall, and the two output contracts."""

from brain.services.eval_extraction import evaluate, format_aggregate, write_report


def rel_row(child_id, target=None, **over):
    base = {
        "email_id": "e1",
        "child_id": child_id,
        "type": "fact",
        "title": f"fact {child_id}",
        "start": "2026-11-03",
        "all_day": True,
        "applies_to": "rona",
    }
    if target:
        base["relation"] = {"kind": "supersedes", "target": target}
    base.update(over)
    return base


def test_relation_reproduced_when_pred_pair_carries_it():
    gold = [rel_row("g_new", "g_old"), rel_row("g_old")]
    pred = [rel_row("p_new", "p_old"), rel_row("p_old")]
    out = format_aggregate(evaluate(gold, pred))
    assert "relation_supersedes_gold: 1" in out
    assert "relation_supersedes_reproduced: 1" in out
    assert "relation_supersedes_rate: 1.0" in out


def test_relation_direction_agnostic():
    gold = [rel_row("g_new", "g_old"), rel_row("g_old")]
    # the pred recorded the link on the superseded child, pointing at the new one
    pred = [rel_row("p_new"), rel_row("p_old", "p_new")]
    out = format_aggregate(evaluate(gold, pred))
    assert "relation_supersedes_reproduced: 1" in out


def test_relation_missing_in_pred_is_not_reproduced():
    gold = [rel_row("g_new", "g_old"), rel_row("g_old")]
    pred = [rel_row("p_new"), rel_row("p_old")]
    out = format_aggregate(evaluate(gold, pred))
    assert "relation_supersedes_reproduced: 0" in out
    assert "relation_supersedes_rate: 0.0" in out


def test_wrong_target_is_not_reproduced():
    gold = [
        rel_row("g_new", "g_old"),
        rel_row("g_old"),
        rel_row("g_third"),
    ]
    # p_new has the right kind but points at p_third instead of p_old
    pred = [
        rel_row("p_new", "p_third"),
        rel_row("p_old"),
        rel_row("p_third"),
    ]
    out = format_aggregate(evaluate(gold, pred))
    assert "matched: 3" in out
    assert "relation_supersedes_gold: 1" in out
    assert "relation_supersedes_reproduced: 0" in out
    assert "relation_supersedes_rate: 0.0" in out


def test_unresolvable_relation_excluded_from_rate():
    gold = [rel_row("g_new", "nonexistent"), rel_row("g_old")]
    pred = [rel_row("p_new"), rel_row("p_old")]
    out = format_aggregate(evaluate(gold, pred))
    assert "relation_supersedes_unresolvable: 1" in out
    assert "relation_supersedes_rate: n/a" in out


def test_date_conflict_recall_counts_flagged_pairs():
    gold = [rel_row("g1", date_conflict=True)]
    assert "date_conflict_recall: 1.0" in format_aggregate(
        evaluate(gold, [rel_row("p1", date_conflict=True)])
    )
    assert "date_conflict_recall: 0.0" in format_aggregate(evaluate(gold, [rel_row("p1")]))


def test_date_conflict_missed_when_pred_unmatched():
    gold = [rel_row("g1", date_conflict=True, title="totally unique g1")]
    pred = [rel_row("p1", title="something else entirely")]
    assert "date_conflict_recall: 0.0" in format_aggregate(evaluate(gold, pred))


def test_notes_fallback_applies_to_gold_only():
    gold = [rel_row("g1", notes="weekday disagrees with the date (date_conflict)")]
    pred = [rel_row("p1", notes="weekday disagrees with the date (date_conflict)")]
    out = format_aggregate(evaluate(gold, pred))
    # gold flagged via notes -> denominator 1; pred NOT flagged (notes ignored) -> recall 0
    assert "date_conflict_gold: 1" in out
    assert "date_conflict_recall: 0.0" in out


def test_aggregate_only_output_has_no_text_or_ids():
    gold = [
        rel_row(
            "g1",
            title="Secret Title אבגד",
            source_quote="classified quote here",
            email_id="school-77",
        )
    ]
    pred = [rel_row("p1", title="Secret Title", source_quote="totally different")]
    out = format_aggregate(evaluate(gold, pred))
    assert "Secret" not in out
    assert "classified" not in out
    assert "school-77" not in out
    assert "totally different" not in out
    for line in out.splitlines():
        key, sep, value = line.partition(": ")
        assert sep, line
        assert key and _is_key(key), line
        assert value == "n/a" or _is_number(value), line


def test_full_report_contains_per_child_detail(tmp_path):
    gold = [
        rel_row("g_new", "g_old", title="Tuesday ends 12:55"),
        rel_row("g_old", title="Tuesday ends 13:35"),
    ]
    pred = [
        rel_row("p_new", "p_old", title="Tuesday ends 12:55"),
        rel_row("p_old", title="Tuesday ends 13:35"),
    ]
    res = evaluate(gold, pred)
    path = tmp_path / "report.md"
    out = write_report(res, path, gold_path="gold.jsonl", pred_path="pred.jsonl")
    text = out.read_text(encoding="utf-8")
    assert "Tuesday ends 12:55" in text
    assert "Tuesday ends 13:35" in text
    assert "gold.jsonl" in text
    assert "## Email `e1`" in text


def _is_key(key: str) -> bool:
    return all(ch.isalnum() or ch == "_" for ch in key)


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True
