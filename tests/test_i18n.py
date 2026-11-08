"""brain.i18n: language codes, string lookup, localized dates."""

from datetime import datetime
from zoneinfo import ZoneInfo

from brain import i18n

TZ = ZoneInfo("Asia/Jerusalem")


def test_normalize_falls_back_to_english():
    assert i18n.normalize("he") == "he"
    assert i18n.normalize("HE-il") == "he"
    assert i18n.normalize("fr") == "en"
    assert i18n.normalize(None) == "en"


def test_lookup_and_format():
    assert i18n.t("he", "card_header", n=3, title="x") == "🧠 מצאתי 3 פריטים בהודעה: x"
    assert i18n.t("en", "card_header", n=3, title="x") == "🧠 Found 3 items in: x"
    assert i18n.t("fr", "no_date") == "no date"


def test_short_date_hebrew_weekday_and_time():
    monday = datetime(2026, 11, 30, tzinfo=TZ)
    assert i18n.short_date(monday, "he") == "יום ב׳ 30.11"
    assert i18n.short_date(monday, "en") == "Mon 30.11"
    assert i18n.short_date(monday.replace(hour=13, minute=35), "he") == "יום ב׳ 30.11 13:35"
    assert i18n.short_date(datetime(2026, 11, 28, tzinfo=TZ), "he") == "שבת 28.11"


def test_every_string_has_both_languages():
    for key, entry in i18n._STRINGS.items():
        assert set(entry) == set(i18n.SUPPORTED), key
