"""Tests for the shared text normalizer (single source for grounding + eval)."""

from brain.services.textnorm import normalize, quote_in_source


def test_geresh_variants_fold_together():
    assert normalize("ז' בחשוון") == normalize("ז׳ בחשוון")
    assert normalize("יום א'") == normalize("יום א׳")


def test_gershayim_variants_fold_together():
    assert normalize('סת"ם') == normalize("סת״ם")
    assert normalize("סת”ם") == normalize('סת"ם')


def test_left_single_quote_folds_to_apostrophe():
    assert normalize("‘ה’") == normalize("'ה'")


def test_maqaf_and_dashes_fold_to_hyphen():
    assert normalize("ה־ו") == normalize("ה-ו")  # maqaf U+05BE vs hyphen
    assert normalize("a–b") == "a-b"  # en dash
    assert normalize("a—b") == "a-b"  # em dash
    assert quote_in_source("ה-ו", "בנות ה־ו")  # email vs WhatsApp separator


def test_nikud_stripped():
    assert normalize("שַׁבָּת") == normalize("שבת")
    assert normalize("בְּקִצוּר") == "בקצור"


def test_bidi_and_invisible_marks_stripped():
    assert normalize("א\u200fב\u200e") == "אב"
    assert normalize("\u202babc\u202c") == "abc"
    assert normalize("a\u200bz") == "az"


def test_whitespace_collapsed_and_casefolded():
    assert normalize("White  Shirt\nDay\t") == "white shirt day"


def test_nfkc_folds_compatibility_forms():
    assert normalize("ﬁ") == "fi"


def test_quote_in_source_positive_with_geresh_mismatch():
    email = "שלום,\nכינוס שאילת גשמים יתקיים ביום א׳ 18.10.\nבברכה"
    assert quote_in_source("כינוס שאילת גשמים יתקיים ביום א'", email)


def test_quote_in_source_spanning_email_linebreak():
    email = "line one\nwhite shirt day\nline three"
    assert quote_in_source("one white shirt", email)


def test_quote_in_source_with_nikud_only_in_source():
    email = "שָׁלוֹם, היום יום חם"
    assert quote_in_source("היום יום חם", email)


def test_quote_in_source_negative():
    assert not quote_in_source("not present here", "some email text")


def test_empty_quote_never_grounded():
    assert not quote_in_source("", "text")
    assert not quote_in_source("   ", "text")
