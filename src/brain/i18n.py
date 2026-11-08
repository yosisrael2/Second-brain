"""User-facing bot strings in the user's language (English / Hebrew).

The language is a memory-layer fact: predicate ``language`` on the ``me``
person entity (``MemoryService.user_language``). English is the default
until it is set, so nothing changes for a user who never sets it.

Keep every user-visible card/answer string here, not inline in the bot,
so a third language is one more column.
"""

from __future__ import annotations

from datetime import datetime

SUPPORTED = ("en", "he")
DEFAULT = "en"

# Monday = 0, matching datetime.weekday().
_WEEKDAY = {
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "he": ("יום ב׳", "יום ג׳", "יום ד׳", "יום ה׳", "יום ו׳", "שבת", "יום א׳"),
}

_STRINGS: dict[str, dict[str, str]] = {
    # proposal card
    "card_header": {
        "en": "🧠 Found {n} items in: {title}",
        "he": "🧠 מצאתי {n} פריטים בהודעה: {title}",
    },
    "card_header_one": {
        "en": "🧠 Found 1 item in: {title}",
        "he": "🧠 מצאתי פריט אחד בהודעה: {title}",
    },
    "card_legend": {
        "en": "📅 event · ✔️ task · 🔔 reminder · ℹ️ for your information",
        "he": "📅 אירוע · ✔️ משימה · 🔔 תזכורת · ℹ️ לידיעה",
    },
    "no_date": {"en": "no date", "he": "ללא תאריך"},
    "who_unknown": {"en": "❓ who is this for?", "he": "❓ למי זה?"},
    "who_nobody": {"en": "not for us", "he": "לא רלוונטי אלינו"},
    "who_me": {"en": "me", "he": "אני"},
    "who_family": {"en": "family", "he": "כל המשפחה"},
    "superseded": {"en": "↩︎ superseded", "he": "↩︎ הוחלף"},
    "v_update_applied": {"en": "↩︎ applied: {changes}", "he": "↩︎ עודכן: {changes}"},
    "v_update": {"en": "↩︎ proposes: {changes}", "he": "↩︎ מציע לעדכן: {changes}"},
    "v_conflict": {
        "en": "⚠️ conflicts — both versions shown",
        "he": "⚠️ סותר פריט קיים, שתי הגרסאות מוצגות",
    },
    "v_cancelled": {"en": "🗑 cancelled", "he": "🗑 בוטל"},
    "v_cancels": {"en": "🗑 cancels: {title}", "he": "🗑 מבטל: {title}"},
    "v_proposes_cancel": {"en": "🗑 proposes cancelling", "he": "🗑 מציע לבטל"},
    "v_more_detail": {"en": "➕ adds detail", "he": "➕ מוסיף פרטים"},
    "due_soon": {"en": "⏰ due soon: {line}", "he": "⏰ מתקרב: {line}"},
    # proposal buttons ({n} = the line number on the card)
    "btn_keep": {"en": "{n} ✅ Keep", "he": "{n} ✅ שמור"},
    "btn_fix": {"en": "{n} ✏️ Fix", "he": "{n} ✏️ תקן"},
    "btn_drop": {"en": "{n} 🗑 Drop", "he": "{n} 🗑 מחק"},
    "btn_keep_all": {"en": "✅ Keep all", "he": "✅ שמור הכל"},
    "btn_undo": {"en": "{n} ↩︎ Undo", "he": "{n} ↩︎ בטל"},
    "btn_confirm_update": {"en": "{n} ✅ Confirm update", "he": "{n} ✅ אשר עדכון"},
    "btn_keep_both": {"en": "{n} ✖ Keep both", "he": "{n} ✖ השאר את שניהם"},
    "btn_keep_existing": {"en": "{n} ✅ Keep existing", "he": "{n} ✅ השאר את הקיים"},
    "btn_keep_new": {"en": "{n} ✅ Keep new", "he": "{n} ✅ השאר את החדש"},
    "btn_confirm_cancel": {"en": "{n} ✅ Confirm cancellation", "he": "{n} ✅ אשר ביטול"},
    "btn_not_cancelled": {"en": "{n} ✖ Not cancelled", "he": "{n} ✖ לא בוטל"},
    # /ask answer
    "ask_answer": {"en": "🧠 <b>Answer</b>", "he": "🧠 <b>תשובה</b>"},
    "ask_sources": {"en": "📚 <b>Sources</b>", "he": "📚 <b>מקורות</b>"},
    "ask_received": {"en": "received {date}", "he": "התקבל {date}"},
    "llm_unavailable": {
        "en": "⚠️ The AI is unavailable right now; try again later.",
        "he": "⚠️ אין כרגע חיבור למודל. נסו שוב בעוד כמה דקות.",
    },
    # scheduled tasks: the schedule in words (brain.services.schedule_spec)
    "sched_daily": {"en": "Every day at {time}", "he": "כל יום ב-{time}"},
    "sched_hourly": {"en": "Every hour at :{minute}", "he": "כל שעה ב-:{minute}"},
    "sched_weekdays": {"en": "Weekdays (Mon–Fri) at {time}", "he": "בימים שני–שישי ב-{time}"},
    "sched_sun_thu": {"en": "Sun–Thu at {time}", "he": "בימים ראשון–חמישי ב-{time}"},
    "sched_weekly_one": {"en": "Every {days} at {time}", "he": "כל יום {days} ב-{time}"},
    "sched_weekly": {"en": "Every {days} at {time}", "he": "בימים {days} ב-{time}"},
    "sched_monthly": {
        "en": "Monthly on day {day} at {time}",
        "he": "כל חודש ב-{day} לחודש ב-{time}",
    },
    "sched_cron": {"en": "Cron: {expr}", "he": "Cron: {expr}"},
    # scheduled tasks: the run result message + /schedules
    "sched_result_header": {"en": "⏰ <b>{name}</b>", "he": "⏰ <b>{name}</b>"},
    "sched_failed": {
        "en": "⚠️ The scheduled task failed: {error}",
        "he": "⚠️ המשימה המתוזמנת נכשלה: {error}",
    },
    "sched_timed_out": {
        "en": "⚠️ The scheduled task took longer than {seconds}s and was stopped.",
        "he": "⚠️ המשימה המתוזמנת נמשכה יותר מ-{seconds} שניות והופסקה.",
    },
    "sched_list_header": {"en": "⏰ <b>Scheduled tasks</b>", "he": "⏰ <b>משימות מתוזמנות</b>"},
    "sched_list_empty": {
        "en": "⏰ No scheduled tasks yet — create one in the cockpit (Scheduled tab).",
        "he": "⏰ אין עדיין משימות מתוזמנות. אפשר ליצור משימה בקוקפיט (לשונית Scheduled).",
    },
    "sched_active": {"en": "Active", "he": "פעילה"},
    "sched_paused": {"en": "Paused", "he": "מושהית"},
    "sched_next": {"en": "next: {when}", "he": "הבאה: {when}"},
    "sched_queued": {"en": "▶️ Queued: {name}", "he": "▶️ נכנסה לתור: {name}"},
    "sched_now_paused": {"en": "⏸ Paused: {name}", "he": "⏸ הושהתה: {name}"},
    "sched_now_resumed": {"en": "▶ Resumed: {name}", "he": "▶ חודשה: {name}"},
    "sched_gone": {"en": "That task no longer exists.", "he": "המשימה כבר לא קיימת."},
    "btn_sched_run": {"en": "{n} ▶️ Run now", "he": "{n} ▶️ הרץ עכשיו"},
    "btn_sched_pause": {"en": "{n} ⏸ Pause", "he": "{n} ⏸ השהה"},
    "btn_sched_resume": {"en": "{n} ▶ Resume", "he": "{n} ▶ חדש"},
    # /language
    "language_set": {
        "en": "🌐 Language set to English.",
        "he": "🌐 השפה הוגדרה לעברית.",
    },
    "language_usage": {
        "en": "Usage: /language he|en (current: {current})",
        "he": "שימוש: ‎/language he|en (כרגע: {current})",
    },
}


def normalize(lang: str | None) -> str:
    """A supported language code; anything else falls back to the default."""
    code = (lang or "").strip().lower()[:2]
    return code if code in SUPPORTED else DEFAULT


def t(lang: str | None, key: str, **values: object) -> str:
    """The string for ``key`` in ``lang`` (English when a translation is missing)."""
    entry = _STRINGS[key]
    template = entry.get(normalize(lang)) or entry[DEFAULT]
    return template.format(**values) if values else template


def weekday(moment: datetime, lang: str | None) -> str:
    return _WEEKDAY[normalize(lang)][moment.weekday()]


def short_date(moment: datetime, lang: str | None) -> str:
    """'Mon 30.11' / 'יום ב׳ 30.11', plus ' 13:35' when the time is not midnight."""
    text = f"{weekday(moment, lang)} {moment.strftime('%d.%m')}"
    if (moment.hour, moment.minute) != (0, 0):
        text += f" {moment.strftime('%H:%M')}"
    return text
