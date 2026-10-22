"""CLI contract for `brain eval-extraction` (run against the tiny fixtures)."""

from pathlib import Path

from brain import cli

FIXTURES = Path(__file__).parent / "fixtures"
GOLD = FIXTURES / "gold_tiny.jsonl"
PRED = FIXTURES / "pred_tiny.jsonl"

# Hand-computed expectations for the tiny fixture set:
# 5 gold children, 5 pred children; 4 matched; g4old missed (FN), p5 invented (FP).
EXPECTED_NUMBERS = {
    "gold_children": "5",
    "pred_children": "5",
    "matched": "4",
    "unmatched_gold": "1",
    "unmatched_pred": "1",
    "precision": "0.8",
    "recall": "0.8",
    "f1": "0.8",
    "date_exact_match_rate": "1.0",
    "applies_to_accuracy": "1.0",
    "grounding_rate": "0.6667",
    "grounding_grounded": "2",
    "grounding_denominator": "3",
    "grounding_unverifiable": "2",
    "relation_supersedes_gold": "1",
    "relation_supersedes_reproduced": "0",
    "relation_supersedes_rate": "0.0",
    "date_conflict_gold": "0",
    "date_conflict_recall": "n/a",
}


def _parse(out: str) -> dict[str, str]:
    parsed = {}
    for line in out.splitlines():
        key, sep, value = line.partition(": ")
        assert sep, f"not key: value -> {line!r}"
        parsed[key] = value
    return parsed


def test_aggregate_only_prints_only_numbers(capsys):
    rc = cli.main(
        [
            "eval-extraction",
            "--gold",
            str(GOLD),
            "--pred",
            str(PRED),
            "--aggregate-only",
        ]
    )
    assert rc == 0
    parsed = _parse(capsys.readouterr().out)
    for key, value in EXPECTED_NUMBERS.items():
        assert parsed.get(key) == value, f"{key}: {parsed.get(key)!r} != {value!r}"
    # no titles, quotes, or email ids anywhere in the aggregate output
    for leak in ("כינוס", "Volunteer", "school-01", "school-02", "Hallucinated"):
        assert leak not in " ".join(parsed)


def test_aggregate_only_writes_no_report_file(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(
        [
            "eval-extraction",
            "--gold",
            str(GOLD),
            "--pred",
            str(PRED),
            "--aggregate-only",
        ]
    )
    assert rc == 0
    assert not (tmp_path / "eval-extraction-report.md").exists()


def test_full_mode_writes_report_and_prints_path(capsys, tmp_path):
    report = tmp_path / "sub" / "report.md"
    rc = cli.main(
        ["eval-extraction", "--gold", str(GOLD), "--pred", str(PRED), "--report", str(report)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "full report written to" in out
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    # full report carries the per-child detail (manager-only)
    assert "כינוס באמפי" in text
    assert "Hallucinated task" in text
    assert "## Email `school-01`" in text
    assert "quote not found" in text  # p2's quote does not verify


def test_missing_gold_file_fails_cleanly(capsys, tmp_path):
    rc = cli.main(
        [
            "eval-extraction",
            "--gold",
            str(tmp_path / "nope.jsonl"),
            "--pred",
            str(PRED),
            "--aggregate-only",
        ]
    )
    assert rc == 1
    assert "failed" in capsys.readouterr().err


def test_bad_json_line_fails_cleanly(capsys, tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"email_id": "e1"}\nnot json\n', encoding="utf-8")
    rc = cli.main(["eval-extraction", "--gold", str(bad), "--pred", str(PRED), "--aggregate-only"])
    assert rc == 1
    assert "failed" in capsys.readouterr().err


def test_missing_required_field_fails_cleanly(capsys, tmp_path):
    bad = tmp_path / "missing.jsonl"
    bad.write_text('{"email_id": "e1", "child_id": "x"}\n', encoding="utf-8")
    rc = cli.main(["eval-extraction", "--gold", str(bad), "--pred", str(PRED), "--aggregate-only"])
    assert rc == 1
    assert "failed" in capsys.readouterr().err


def test_aware_pred_runs_via_cli_default_timezone(capsys):
    # an offset-bearing pred is converted to the configured local zone, not rejected
    pred = Path(__file__).parent / "fixtures" / "pred_aware_tiny.jsonl"
    pred.write_text(
        '{"email_id": "school-01", "child_id": "p1", "type": "event", '
        '"title": "כינוס באמפי", "start": "2026-10-17T21:00:00+00:00", '
        '"all_day": true, "applies_to": "itamar"}\n',
        encoding="utf-8",
    )
    rc = cli.main(["eval-extraction", "--gold", str(GOLD), "--pred", str(pred), "--aggregate-only"])
    assert rc == 0


def test_invalid_timezone_fails_cleanly(capsys):
    rc = cli.main(
        [
            "eval-extraction",
            "--gold",
            str(GOLD),
            "--pred",
            str(PRED),
            "--timezone",
            "Not/AZone",
            "--aggregate-only",
        ]
    )
    assert rc == 1
    assert "failed" in capsys.readouterr().err


def test_emails_sidecar_feeds_grounding(capsys, tmp_path):
    sidecar = tmp_path / "emails.jsonl"
    sidecar.write_text(
        '{"email_id": "school-02", "email_text": "שלום, מיום א\' בנובמבר '
        "יסתיים יום ג' בשעה 12:55. תודה\"}\n",
        encoding="utf-8",
    )
    rc = cli.main(
        [
            "eval-extraction",
            "--gold",
            str(GOLD),
            "--pred",
            str(PRED),
            "--emails",
            str(sidecar),
            "--aggregate-only",
        ]
    )
    assert rc == 0
    parsed = _parse(capsys.readouterr().out)
    # school-02 now has email text: p4's quote verifies, p5's still does not
    assert parsed["grounding_denominator"] == "5"
    assert parsed["grounding_grounded"] == "3"
    assert parsed["grounding_rate"] == "0.6"
    assert parsed["grounding_unverifiable"] == "0"
