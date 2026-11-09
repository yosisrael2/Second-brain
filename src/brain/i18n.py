"""User-facing bot strings in the user's language (English / Hebrew).

The language is a memory-layer fact: predicate ``language`` on the ``me``
person entity (``MemoryService.user_language``). English is the default
until it is set, so nothing changes for a user who never sets it.

Keep every user-visible card/answer string here, not inline in the bot,
so a third language is one more column.
"""

from __future__ import annotations

import re
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
    "due_soon": {
        "en": "⏰ due soon: {line}",
        "he": "⏰ מתקרב, ועדיין מחכה לאישור שלכם:\n{line}",
    },
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
    "sched_hourly": {"en": "Every hour at :{minute}", "he": "כל שעה, בדקה {minute}"},
    "sched_weekdays": {"en": "Weekdays (Mon–Fri) at {time}", "he": "בימים שני–שישי ב-{time}"},
    "sched_sun_thu": {"en": "Sun–Thu at {time}", "he": "בימים ראשון–חמישי ב-{time}"},
    "sched_weekly_one": {"en": "Every {days} at {time}", "he": "כל יום {days} ב-{time}"},
    "sched_weekly": {"en": "Every {days} at {time}", "he": "בימים {days} ב-{time}"},
    "sched_monthly": {
        "en": "Monthly on day {day} at {time}",
        "he": "כל חודש ב-{day} לחודש ב-{time}",
    },
    "sched_cron": {"en": "Cron: {expr}", "he": "תזמון מותאם אישית ({expr})"},
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
        "he": "⏰ אין עדיין משימות מתוזמנות. אפשר ליצור משימה חדשה בממשק הניהול במחשב.",
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
    "btn_sched_resume": {"en": "{n} ▶ Resume", "he": "{n} ▶ המשך"},
    # /language
    "language_set": {
        "en": "🌐 Language set to English.",
        "he": "🌐 השפה הוגדרה לעברית.",
    },
    "language_usage": {
        "en": "Usage: /language he|en (current: {current})",
        "he": (
            "🌐 השפה כרגע: {current_name}.\n"
            "לעברית: \u200e/language he\nלאנגלית: \u200e/language en"
        ),
    },
    "lang_name_en": {"en": "English", "he": "אנגלית"},
    "lang_name_he": {"en": "Hebrew", "he": "עברית"},
    "language_failed": {
        "en": "⚠️ {error}",
        "he": "⚠️ לא הצלחתי לשמור את השפה. נסו שוב בעוד רגע.",
    },
    # --- everything below: the rest of the bot (telegram_bot.py) ----------------
    # Hebrew commands carry U+200E (LRM) so the "/" stays on the left of the
    # command inside right-to-left text.
    "start": {
        "en": (
            "👋 Second Brain bot ready.\n"
            "Paste an email / WhatsApp thread / calendar event — I'll capture it.\n"
            "Any question ending in '?' goes to /ask.\n"
            "Commands: /search, /ask, /review, /recent, /profile, /remember, /schedules, "
            "/status, /help"
        ),
        "he": (
            "👋 שלום! אני המוח השני של המשפחה.\n"
            "📥 העבירו או הדביקו לי מייל, הודעת וואטסאפ או הזמנה ליומן, ואני אשמור ואוציא"
            " מהם אירועים ומשימות.\n"
            "❓ שאלה שמסתיימת בסימן שאלה? אענה לפי מה ששמור אצלי.\n"
            "📋 כל האפשרויות: ‎/help"
        ),
    },
    "help": {
        "en": (
            "Commands:\n"
            "— paste any text: captured (email/WhatsApp/calendar detected)\n"
            "— any question ending in '?': answered from the memory\n"
            "/search <words> — semantic search\n"
            "/ask <question> — ask the memory\n"
            "/review — walk the inbox one card at a time\n"
            "/recent — last 10 items\n"
            "/profile — your kids and remembered facts (with remove buttons)\n"
            "/remember <fact> — remember something about your world\n"
            "/schedules — scheduled tasks (run now / pause / resume)\n"
            "/status — component health (cockpit)\n"
            "/start — this greeting"
        ),
        "he": (
            "איך משתמשים בי:\n"
            "📥 מדביקים או מעבירים הודעה (מייל, וואטסאפ, הזמנה ליומן) ואני שומר אותה.\n"
            "❓ כותבים שאלה שמסתיימת בסימן שאלה ואני עונה לפי מה ששמור.\n"
            "\n"
            "פקודות:\n"
            "🔍 חיפוש: ‎/search ואחריו מה לחפש\n"
            "❓ שאלה: ‎/ask ואחריו השאלה\n"
            "📥 מעבר על הודעות שמחכות לבדיקה: ‎/review\n"
            "🕘 עשרת הפריטים האחרונים: ‎/recent\n"
            "👤 פרטי המשפחה ומה שביקשתם שאזכור: ‎/profile\n"
            "🧠 לזכור משהו: ‎/remember ואחריו מה לזכור\n"
            "⏰ משימות מתוזמנות: ‎/schedules\n"
            "🩺 האם הכל עובד: ‎/status\n"
            "🌐 לעבור לאנגלית: ‎/language en"
        ),
    },
    # Telegram's command menu (set_my_commands)
    "cmd_ask": {"en": "Ask your memory a question", "he": "לשאול שאלה"},
    "cmd_search": {"en": "Semantic search", "he": "חיפוש"},
    "cmd_review": {"en": "Review inbox items", "he": "מעבר על הודעות שמחכות לבדיקה"},
    "cmd_recent": {"en": "Show the last 10 items", "he": "עשרת הפריטים האחרונים"},
    "cmd_profile": {"en": "Your kids and remembered facts", "he": "פרטי המשפחה"},
    "cmd_remember": {"en": "Remember a fact: /remember <text>", "he": "לזכור משהו"},
    "cmd_schedules": {"en": "Scheduled tasks: run now, pause, resume", "he": "משימות מתוזמנות"},
    "cmd_status": {"en": "Component health", "he": "האם הכל עובד"},
    "cmd_language": {"en": "Bot language: /language he | en", "he": "שפת הבוט"},
    "cmd_help": {"en": "How to use the bot", "he": "איך משתמשים"},
    # capture card
    "saved": {"en": "✅ <b>Saved</b> · {kind}", "he": "✅ <b>נשמר</b> · {kind}"},
    "already_saved": {
        "en": "ℹ️ <b>Already saved</b> · {kind}",
        "he": "ℹ️ <b>כבר שמור אצלי</b> · {kind}",
    },
    "n_items": {"en": " · {n} items", "he": " · {n} פריטים"},
    "kind_email": {"en": "Email", "he": "מייל"},
    "kind_whatsapp": {"en": "WhatsApp", "he": "וואטסאפ"},
    "kind_calendar": {"en": "Event", "he": "אירוע"},
    "kind_event": {"en": "Event", "he": "אירוע"},
    "kind_note": {"en": "Note", "he": "פתק"},
    "kind_task": {"en": "Task", "he": "משימה"},
    "kind_idea": {"en": "Idea", "he": "רעיון"},
    "kind_decision": {"en": "Decision", "he": "החלטה"},
    "kind_reference": {"en": "Reference", "he": "חומר עזר"},
    "kind_reminder": {"en": "Reminder", "he": "תזכורת"},
    "kind_fact": {"en": "Fact", "he": "לידיעה"},
    "act_now": {"en": "⏰ Now", "he": "⏰ דחוף"},
    "act_later": {"en": "🕐 Later", "he": "🕐 לא דחוף"},
    "field_from": {"en": "From: {value}", "he": "מאת: {value}"},
    "field_subject": {"en": "Subject: {value}", "he": "נושא: {value}"},
    "field_when": {"en": "When: {value}", "he": "מתי: {value}"},
    "field_where": {"en": "Where: {value}", "he": "איפה: {value}"},
    "needs_review": {
        "en": "🔎 Needs review ({percent}%)",
        "he": "🔎 לא בטוח שסיווגתי נכון, אפשר לתקן בכפתורים.",
    },
    "btn_file": {"en": "✅ File", "he": "✅ תייק"},
    "btn_type": {"en": "🏷 Type", "he": "🏷 שנה סוג"},
    "btn_delete": {"en": "🗑 Delete", "he": "🗑 מחק"},
    "btn_delete_original": {"en": "🗑 Delete original", "he": "🗑 מחק את השמור"},
    "btn_undo_capture": {"en": "↩️ Undo", "he": "↩️ בטל שמירה"},
    "btn_next": {"en": "Next ▶️", "he": "הבא ◀️"},
    "btn_back": {"en": "« Back", "he": "» חזרה"},
    "btn_restore": {"en": "♻️ Restore", "he": "♻️ שחזר"},
    "pick_type": {"en": "Pick a type:", "he": "🏷 מה זה? בחרו סוג:"},
    "type_set": {"en": "✏️ Type: {label}", "he": "✏️ הסוג עודכן: {label}"},
    "filed": {"en": "📁 Filed {n} item(s).", "he": "📁 תויקו {n} פריטים."},
    "filed_one": {"en": "📁 Filed {n} item(s).", "he": "📁 תויק, הפריט יצא מרשימת הבדיקה."},
    "undone": {"en": "↩️ Undone {n} item(s).", "he": "↩️ השמירה בוטלה, {n} פריטים נמחקו."},
    "undone_one": {"en": "↩️ Undone {n} item(s).", "he": "↩️ השמירה בוטלה והפריט נמחק."},
    "undone_zero": {"en": "↩️ Undone {n} item(s).", "he": "↩️ השמירה כבר בוטלה קודם."},
    "deleted": {"en": "🗑 Deleted {n} item(s).", "he": "🗑 נמחקו {n} פריטים."},
    "deleted_one": {"en": "🗑 Deleted {n} item(s).", "he": "🗑 נמחק."},
    "deleted_zero": {"en": "🗑 Deleted {n} item(s).", "he": "🗑 זה כבר נמחק קודם."},
    "deleted_card": {"en": "🗑 Deleted", "he": "🗑 נמחק"},
    # WhatsApp paste
    "wa_thread": {
        "en": "💬 WhatsApp thread with {n} messages — save as one item or separately?",
        "he": "💬 שרשור וואטסאפ עם {n} הודעות. לשמור את כולו כפריט אחד, או כל הודעה בנפרד?",
    },
    "btn_wa_join": {"en": "Save as 1 item", "he": "💾 הכל כפריט אחד"},
    "btn_wa_split": {"en": "Save separately", "he": "✂️ כל הודעה בנפרד"},
    # documents / attachments / unsupported
    "doc_file": {"en": "file: {name}", "he": "קובץ: {name}"},
    "att_reading": {"en": "📎 saved, reading…", "he": "📎 הקובץ נשמר, אני קורא אותו…"},
    "att_saved": {"en": "📎 attachment saved", "he": "📎 הקובץ נשמר"},
    "att_unreadable": {
        "en": "📎 {name} — saved (searchable by filename only)",
        "he": "📎 {name}: נשמר, אבל לא הצלחתי לקרוא את התוכן. אפשר למצוא אותו לפי שם הקובץ.",
    },
    "att_too_large": {
        "en": "📎 {name} — too large to read (over 20 MB), not stored",
        "he": "📎 {name}: הקובץ גדול מדי (מעל 20 מגה) ולא נשמר.",
    },
    "att_failed": {
        "en": "📎 {name} — ⚠️ reading failed; recorded in ops",
        "he": "📎 {name}: ⚠️ הקובץ נשמר, אבל לא הצלחתי לקרוא אותו.",
    },
    "unsupported_voice": {
        "en": "😕 Voice and video are not supported yet — paste the text instead.",
        "he": "😕 עדיין אין לי תמיכה בהקלטות ובווידאו. אפשר לכתוב או להדביק את הטקסט.",
    },
    "unsupported_files": {
        "en": "😕 Files other than .ics are not supported yet — paste the text instead.",
        "he": "😕 עדיין אין לי תמיכה בסוג הקובץ הזה. אפשר להדביק את הטקסט.",
    },
    "unsupported_other": {
        "en": "😕 {what} are not supported yet — paste the text instead.",
        "he": "😕 עדיין אין לי תמיכה בזה. אפשר להדביק את הטקסט.",
    },
    # /search, /recent, /ask usage
    "search_usage": {
        "en": "Usage: /search <words>",
        "he": "🔍 מה לחפש? כתבו למשל: ‎/search אסיפת הורים",
    },
    "search_none": {
        "en": "🔍 No results for '{query}'.",
        "he": "🔍 לא מצאתי שום דבר עבור: {query}",
    },
    "search_header": {
        "en": "🔍 {n} result(s) for '{query}':",
        "he": "🔍 מצאתי {n} תוצאות עבור: {query}",
    },
    "search_header_one": {
        "en": "🔍 {n} result(s) for '{query}':",
        "he": "🔍 מצאתי תוצאה אחת עבור: {query}",
    },
    "open_hint": {"en": "", "he": "📎 לפתיחת פריט לחצו על המספר שלו."},
    "ask_usage": {
        "en": "Usage: /ask <question>",
        "he": "❓ מה לשאול? כתבו שאלה שמסתיימת בסימן שאלה, למשל: מתי האסיפה?",
    },
    "recent_empty": {
        "en": "No items yet — paste something!",
        "he": "עוד אין כאן כלום. הדביקו או העבירו לי הודעה ואשמור אותה.",
    },
    "recent_header": {"en": "🕘 Last {n} item(s):", "he": "🕘 {n} הפריטים האחרונים:"},
    "recent_header_one": {"en": "🕘 Last {n} item(s):", "he": "🕘 הפריט האחרון:"},
    # /review
    "inbox_empty": {
        "en": "📥 Inbox is empty.",
        "he": "📥 אין הודעות שמחכות לבדיקה. הכל מסודר!",
    },
    "inbox_end": {"en": "📥 End of inbox.", "he": "📥 זהו, עברנו על כל ההודעות שמחכות לבדיקה."},
    "inbox_position": {"en": "📥 Inbox ({i}/{n})", "he": "📥 מחכה לבדיקה ({i} מתוך {n})"},
    # /profile + /remember
    "profile_title": {"en": "👤 <b>Profile</b>", "he": "👤 <b>פרטי המשפחה</b>"},
    "profile_remembered": {"en": "🧠 <b>Remembered</b>", "he": "🧠 <b>מה שביקשתם שאזכור</b>"},
    "profile_hint": {"en": "", "he": "✖ כדי למחוק שורה, לחצו על ✖ עם המספר שלה."},
    "profile_empty": {
        "en": (
            "👤 Profile is empty.\n"
            "Kids are set up once via the seed CLI:\n"
            "  brain memory seed-kid --name Rona --gender girl --school <school> "
            "--grade 5 --class <class> --teacher <teacher>\n"
            "/remember <text> stores a free-text note (not kid facts)."
        ),
        "he": (
            "👤 עוד אין לי פרטים על המשפחה.\n"
            "את פרטי הילדים (כיתה, מחנכת, בית ספר) מגדירים פעם אחת בממשק הניהול.\n"
            "כדי שאזכור משהו, כתבו ‎/remember ואחריו מה לזכור."
        ),
    },
    "memory_missing": {
        "en": "Memory layer is not installed yet.",
        "he": "⚠️ הזיכרון עוד לא זמין.",
    },
    "fact_gone": {"en": "⌛ That fact was already removed.", "he": "⌛ השורה הזאת כבר נמחקה."},
    "fact_removed": {"en": "🗑 Removed (history kept).", "he": "🗑 נמחק."},
    "remember_usage": {
        "en": "Usage: /remember <fact> — e.g. /remember Itamar plays basketball",
        "he": "🧠 מה לזכור? כתבו למשל: ‎/remember איתמר משחק כדורסל בימי שני",
    },
    "remembered": {"en": "🧠 <b>Remembered</b>\n{text}", "he": "🧠 <b>אזכור את זה</b>\n{text}"},
    # profile labels (unknown predicates/values show as stored)
    "pred_gender": {"en": "gender", "he": "מין"},
    "pred_grade": {"en": "grade", "he": "שכבה"},
    "pred_class": {"en": "class", "he": "כיתה"},
    "pred_school": {"en": "school", "he": "בית ספר"},
    "pred_teacher": {"en": "teacher", "he": "מחנכת"},
    "pred_birthday": {"en": "birthday", "he": "יום הולדת"},
    "pred_relation": {"en": "relation", "he": "קרבה"},
    "pred_work": {"en": "work", "he": "עבודה"},
    "pred_email": {"en": "email", "he": "מייל"},
    "pred_phone": {"en": "phone", "he": "טלפון"},
    "pred_note": {"en": "note", "he": "הערה"},
    "pred_connection:sibling": {"en": "sibling", "he": "אח/אחות של"},
    "pred_connection:parent": {"en": "parent", "he": "הורה של"},
    "pred_connection:spouse": {"en": "spouse", "he": "בן/בת זוג של"},
    "pred_connection:child": {"en": "child", "he": "ילד/ה של"},
    "val_boy": {"en": "boy", "he": "בן"},
    "val_girl": {"en": "girl", "he": "בת"},
    "val_man": {"en": "man", "he": "גבר"},
    "val_woman": {"en": "woman", "he": "אישה"},
    # /status
    "status_missing": {"en": "cockpit not installed yet", "he": "⚪ בדיקת המערכת לא זמינה כרגע."},
    "status_healthy": {"en": "🟢 brain: healthy", "he": "🟢 הכל עובד"},
    "status_degraded": {"en": "🟠 brain: degraded", "he": "🟠 חלק מהדברים עובדים חלקית"},
    "status_down": {"en": "🔴 brain: down", "he": "🔴 משהו לא עובד כרגע"},
    "health_ok": {"en": "ok", "he": "תקין"},
    "health_degraded": {"en": "degraded", "he": "עובד חלקית"},
    "health_down": {"en": "down", "he": "לא עובד"},
    "health_disabled": {"en": "disabled", "he": "כבוי"},
    "health_not probed": {"en": "not probed", "he": "לא נבדק"},
    "comp_api": {"en": "api", "he": "השרת"},
    "comp_postgres": {"en": "postgres", "he": "מסד הנתונים"},
    "comp_qdrant": {"en": "qdrant", "he": "אינדקס החיפוש"},
    "comp_embedder": {"en": "embedder", "he": "הכנת טקסט לחיפוש"},
    "comp_jev": {"en": "jev", "he": "סיווג הודעות"},
    "comp_llm": {"en": "llm", "he": "מודל השפה"},
    "comp_telegram": {"en": "telegram", "he": "טלגרם"},
    "comp_email": {"en": "email", "he": "מייל"},
    "comp_attachments": {"en": "attachments", "he": "קבצים מצורפים"},
    "comp_enrichment": {"en": "enrichment", "he": "חילוץ אירועים ומשימות"},
    "comp_triage": {"en": "triage", "he": "מיון הודעות"},
    "comp_docker": {"en": "docker", "he": "מכולות"},
    "comp_background": {"en": "background", "he": "עבודות רקע"},
    "comp_disk": {"en": "disk", "he": "דיסק"},
    "comp_whatsapp": {"en": "whatsapp", "he": "וואטסאפ"},
    # generic button / state replies
    "btn_unknown": {
        "en": "Unknown action — the button is stale or invalid.",
        "he": "⌛ הכפתור הזה כבר לא פעיל. שלחו שוב את הפקודה כדי לקבל כפתורים חדשים.",
    },
    "btn_expired": {
        "en": "⌛ That button expired (valid 10 minutes) — send the message again.",
        "he": "⌛ הכפתורים פעילים 10 דקות בלבד. שלחו שוב את ההודעה או הפקודה.",
    },
    "type_unknown": {
        "en": "Unknown type — the button is stale or invalid.",
        "he": "⌛ הכפתור הזה כבר לא פעיל. שלחו שוב את הפקודה כדי לקבל כפתורים חדשים.",
    },
    "item_deleted": {"en": "⌛ That item was deleted.", "he": "⌛ הפריט הזה נמחק."},
    # proposal card actions
    "card_moved_on": {
        "en": "⌛ That button expired — the card moved on.",
        "he": "⌛ הכפתור הזה כבר לא פעיל, הכרטיס התעדכן.",
    },
    "already_handled": {
        "en": "⌛ already handled — the card moved on.",
        "he": "⌛ כבר טיפלתם בזה, הכרטיס התעדכן.",
    },
    "kept_title": {"en": "✅ Kept: {title}", "he": "✅ נשמר: {title}"},
    "dropped_title": {
        "en": "🗑 Dropped (never returns): {title}",
        "he": "🗑 נמחק ולא יוצע שוב: {title}",
    },
    "kept_n": {"en": "✅ Kept {n} item(s).", "he": "✅ נשמרו {n} פריטים."},
    "kept_n_one": {"en": "✅ Kept {n} item(s).", "he": "✅ נשמר פריט אחד."},
    "kept_n_zero": {"en": "✅ Kept {n} item(s).", "he": "✅ לא נשאר כאן מה לשמור."},
    "all_done": {"en": " ✓ all done here.", "he": "✓ סיימנו עם ההודעה הזאת."},
    # relation transitions (supersede / recon verdicts)
    "rel_undone": {
        "en": "↩️ Undone — the earlier item is current again.",
        "he": "↩️ בוטל, הגרסה הקודמת חזרה להיות בתוקף.",
    },
    "rel_confirmed": {
        "en": "✅ Confirmed — the old version is struck through (undo anytime).",
        "he": "✅ אושר. הגרסה הישנה סומנה כלא בתוקף (אפשר להחזיר בכל רגע).",
    },
    "rel_kept_both": {
        "en": "✖ Keeping both — proposal discarded.",
        "he": "✖ בסדר, שניהם נשארים.",
    },
    "rel_kept_new": {
        "en": "✅ Keeping the new version — the old one is struck through (undo anytime).",
        "he": "✅ נשארת הגרסה החדשה. הישנה סומנה כלא בתוקף (אפשר להחזיר בכל רגע).",
    },
    "rel_kept_existing": {
        "en": "✅ Keeping the existing version — proposal discarded.",
        "he": "✅ נשארת הגרסה הקיימת.",
    },
    "rel_cancelled": {
        "en": "🗑 Cancelled — the item is struck through (restore anytime).",
        "he": "🗑 סומן כמבוטל (אפשר לשחזר בכל רגע).",
    },
    "rel_not_cancelled": {
        "en": "✖ Not cancelled — proposal discarded.",
        "he": "✖ לא בוטל, הכל נשאר כמו שהיה.",
    },
    "rel_restored": {
        "en": "♻️ Restored — back in /recent.",
        "he": "♻️ שוחזר, הפריט חזר להופיע ברשימות.",
    },
    # relations_view card lines
    "rv_replaced": {
        "en": "↩︎ replaced: {old} → {new}",
        "he": "↩︎ עודכן: במקום „{old}” עכשיו „{new}”",
    },
    "rv_maybe_replaces": {
        "en": "❓ maybe replaces: {old} — confirm or keep both",
        "he": "❓ אולי זה מחליף את: {old}\nאשרו את ההחלפה, או השאירו את שניהם.",
    },
    "rv_replaced_by": {"en": "↪︎ replaced by: {new}", "he": "↪︎ הוחלף ב: {new}"},
    "rv_btn_undo": {"en": "↩︎ Undo", "he": "↩︎ בטל את העדכון"},
    "rv_btn_confirm": {"en": "✅ Confirm replace", "he": "✅ אשר החלפה"},
    "rv_btn_keep_both": {"en": "✖ Keep both", "he": "✖ השאר את שניהם"},
    "rv_which": {
        "en": "which is right?\n  existing: {old}\n  new: {new}",
        "he": "❓ מה נכון?\n  הקיים: {old}\n  החדש: {new}",
    },
    "rv_detail": {"en": "🔔 more detail captured: {other}", "he": "🔔 יש פרטים נוספים: {other}"},
    "v_change": {"en": "{field}: {old} → {new}", "he": "{field}: {new} (במקום {old})"},
    "vf_time": {"en": "time", "he": "שעה"},
    "vf_end": {"en": "end", "he": "סיום"},
    "vf_location": {"en": "location", "he": "מקום"},
    "vf_amount": {"en": "amount", "he": "סכום"},
    "vf_when": {"en": "when", "he": "מתי"},
    # the Fix flow
    "fix_no_user": {
        "en": "Unknown user — cannot start an edit.",
        "he": "⚠️ לא הצלחתי לזהות מי לחץ. נסו שוב.",
    },
    "fix_title_prompt": {
        "en": "✏️ New title for: {title}\nType the new title (or 'cancel').",
        "he": "✏️ מתקנים את: {title}\nכתבו את השם הנכון (או „ביטול” כדי לצאת).",
    },
    "fix_cancelled": {
        "en": "✏️ Fix cancelled — nothing changed.",
        "he": "✏️ התיקון בוטל, שום דבר לא השתנה.",
    },
    "fix_date_prompt": {
        "en": (
            "✏️ Title will be: {title}\n"
            "Now the date: dd.mm / 'יום חמישי 29.10 13:00' / 'keep' to leave it "
            "unchanged ('cancel' aborts)."
        ),
        "he": (
            "✏️ השם החדש: {title}\n"
            "ועכשיו התאריך, למשל 29.10 או „יום חמישי 29.10 13:00”.\n"
            "כדי להשאיר את התאריך כמו שהוא כתבו „השאר” („ביטול” כדי לצאת)."
        ),
    },
    "fix_item_gone": {
        "en": "⌛ That item is gone — fix aborted.",
        "he": "⌛ הפריט הזה כבר לא קיים, התיקון בוטל.",
    },
    "fix_retry": {
        "en": "✏️ {error}\nTry again (or 'cancel').",
        "he": "✏️ {error}\nנסו שוב (או „ביטול”).",
    },
    "fix_done": {
        "en": "✏️ Fixed: {title}\nYour edit is locked — extraction will not overwrite it.",
        "he": "✏️ תוקן: {title}\nהתיקון שלכם נשמר ולא יידרס בעדכונים הבאים.",
    },
    "fixerr_empty_title": {"en": "the title cannot be empty", "he": "השם לא יכול להיות ריק."},
    "fixerr_long_title": {
        "en": "the title is too long (max 200 characters)",
        "he": "השם ארוך מדי (עד 200 תווים).",
    },
    "fixerr_no_source": {
        "en": "no source to resolve the date against",
        "he": "לא מצאתי את ההודעה המקורית, אז אני לא יכול להבין את התאריך.",
    },
    "fixerr_bad_date": {
        "en": "could not read that date — try dd.mm (e.g. 29.10) or 'יום חמישי 29.10'",
        "he": "לא הבנתי את התאריך. נסו למשל 29.10 או „יום חמישי 29.10”.",
    },
}

# What the user may type in the Fix flow (either language, any case).
CANCEL_WORDS = frozenset({"cancel", "ביטול", "בטל"})
KEEP_WORDS = frozenset({"keep", "-", "השאר", "להשאיר"})

_FSI = "⁨"  # FIRST STRONG ISOLATE
_PDI = "⁩"  # POP DIRECTIONAL ISOLATE
_LATIN = re.compile("[A-Za-z]")


def normalize(lang: str | None) -> str:
    """A supported language code; anything else falls back to the default."""
    code = (lang or "").strip().lower()[:2]
    return code if code in SUPPORTED else DEFAULT


def t(lang: str | None, key: str, **values: object) -> str:
    """The string for ``key`` in ``lang`` (English when a translation is missing)."""
    entry = _STRINGS[key]
    template = entry.get(normalize(lang)) or entry[DEFAULT]
    return template.format(**values) if values else template


def tn(lang: str | None, key: str, n: int, **values: object) -> str:
    """Count-aware lookup: ``key_zero`` / ``key_one`` when they exist (Hebrew
    says "פריט אחד", not "1 פריטים"), else ``key``; ``n`` is passed as {n}."""
    for suffix, wanted in (("_zero", 0), ("_one", 1)):
        if n == wanted and key + suffix in _STRINGS:
            return t(lang, key + suffix, n=n, **values)
    return t(lang, key, n=n, **values)


def label(lang: str | None, key: str, default: str) -> str:
    """The translation of an optional key (a predicate, a value, a component
    name), or ``default`` when there is none — data shows as stored."""
    entry = _STRINGS.get(key)
    if entry is None:
        return default
    return entry.get(normalize(lang)) or entry[DEFAULT]


def iso(text: str, lang: str | None) -> str:
    """Hebrew only: wrap a Latin-script fragment (a filename, an English
    subject, an error) in a first-strong isolate, so the fragment never
    flips the direction of the right-to-left line around it — Telegram
    picks a line's direction from its first strong letter. Text without
    Latin letters, and every English reply, is returned unchanged."""
    if normalize(lang) != "he" or not _LATIN.search(text):
        return text
    return f"{_FSI}{text}{_PDI}"


def weekday(moment: datetime, lang: str | None) -> str:
    return _WEEKDAY[normalize(lang)][moment.weekday()]


def short_date(moment: datetime, lang: str | None) -> str:
    """'Mon 30.11' / 'יום ב׳ 30.11', plus ' 13:35' when the time is not midnight."""
    text = f"{weekday(moment, lang)} {moment.strftime('%d.%m')}"
    if (moment.hour, moment.minute) != (0, 0):
        text += f" {moment.strftime('%H:%M')}"
    return text
