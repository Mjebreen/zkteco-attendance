"""Tiny i18n layer: English + Arabic strings, RTL flag, localized date formatting.

`resolve_lang()` picks ?lang= > cookie > Accept-Language > DEFAULT_LANG.
Templates get `t(key, **kw)` and `fmt_date(d, style)` helpers.
"""

from __future__ import annotations

from datetime import date, datetime

LANGS = ("en", "ar")
RTL = {"ar"}

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "app_title": "Attendance",
        "dashboard": "Dashboard",
        "print_save": "Print / Save as PDF",
        "download_pdf": "Download PDF",
        "export_csv": "Export CSV",
        "refresh": "Refresh",
        "from": "From",
        "to": "To",
        "show": "Show",
        "yesterday": "Yesterday",
        "today": "Today",
        "last_7": "Last 7 days",
        "last_30": "Last 30 days",
        "this_month": "This month",
        "sync_now": "Sync now",
        "syncing": "Syncing…",
        "last_synced": "Last synced",
        "min_ago": "{n} min ago",
        "just_now": "less than a minute ago",
        "never_synced": "Never synced — waiting for the collector",
        "stale": "stale",
        "last_error": "last error",
        "synced_ok": "Synced with the device just now.",
        "sync_queued": "Sync requested — the collector will pull the device within a few seconds.",
        "sync_failed": "Sync failed",
        "total_employees": "Total employees",
        "present": "Present",
        "absent": "Absent",
        "total_hours": "Total hours",
        "currently_in": "Currently in",
        "checked_in": "Checked in",
        "not_checked_out": "Hasn't checked out yet",
        "no_checkout": "No check-out recorded",
        "checked_out": "Checked out",
        "live": "Live",
        "live_note": "Live hours count from the first punch until now for employees who haven't checked out. Reports use completed check-in/check-out pairs only.",
        "employee": "Employee",
        "id": "ID",
        "department": "Department",
        "all_departments": "All departments",
        "no_department": "No department",
        "first_in": "First in",
        "last_out": "Last out",
        "hours": "Hours",
        "status": "Status",
        "target": "Target",
        "target_hours": "{h} h target",
        "target_met": "Target met",
        "below_target": "Below target",
        "late": "Late",
        "early": "Early",
        "late_after": "late after {t}",
        "early_before": "early before {t}",
        "turn_at": "turn at {t}",
        "search_placeholder": "Search by name, ID or department…",
        "no_results": "No employees match your search.",
        "no_one_attended": "No one attended in this window.",
        "everyone_attended": "Everyone attended.",
        "no_employees": "No employees in the database yet. The collector adds them on its first pull.",
        "date_range": "Date range",
        "days": "days",
        "employees": "Employees",
        "avg_present_day": "Avg present / day",
        "attendance_summary": "Attendance summary",
        "attendance": "Attendance",
        "days_present": "Days present",
        "days_absent": "Days absent",
        "avg_hrs_day": "Avg hrs / day",
        "rate": "Rate",
        "by_department": "By department",
        "presence_per_day": "Presence per day",
        "shift_window": "Shift window",
        "next_day": "next day",
        "shift_days_each": "shift-days (each {a} → next day {b})",
        "settings": "Settings",
        "departments": "Departments",
        "add_department": "Add department",
        "department_name": "Department name",
        "add": "Add",
        "rename": "Rename",
        "save": "Save",
        "delete": "Delete",
        "confirm_delete_dept": "Delete this department? Its employees will be left without a department.",
        "no_departments": "No departments yet. Add one above, then assign employees below.",
        "members": "members",
        "device_name": "Device name",
        "display_name": "Display name",
        "display_name_hint": "Optional — overrides the device name in this system only.",
        "active": "Active",
        "inactive": "Inactive",
        "show_inactive": "Show inactive employees",
        "saved": "Saved.",
        "deleted": "Deleted.",
        "error": "Error",
        "back_dashboard": "Back to dashboard",
        "employee_profile": "Employee profile",
        "view_profile": "View profile",
        "date": "Date",
        "day": "Day",
        "punches": "Punches",
        "daily_report": "Daily Attendance Report",
        "range_report": "Attendance Summary",
        "attendance_report": "Attendance Report",
        "daily_record": "Daily attendance record",
        "n_day_summary": "{n}-day attendance summary",
        "attendance_by_employee": "Attendance by employee",
        "print_preview": "Print preview",
        "page": "Page",
        "of": "of",
        "dark_mode": "Dark mode",
        "light_mode": "Light mode",
        "switch_lang": "العربية",
        "loaded": "loaded",
        "device": "Device",
        "weekday": "Weekday",
        "open": "open",
        "no_data": "No data for this period.",
        "employee_not_found": "Employee not found.",
        "history": "History",
        "profile_hint": "Every shift day in the range, including absences.",
        "dept_exists": "A department with that name already exists.",
        "name_required": "A name is required.",
    },
    "ar": {
        "app_title": "الحضور",
        "dashboard": "لوحة التحكم",
        "print_save": "طباعة / حفظ PDF",
        "download_pdf": "تنزيل PDF",
        "export_csv": "تصدير CSV",
        "refresh": "تحديث",
        "from": "من",
        "to": "إلى",
        "show": "عرض",
        "yesterday": "أمس",
        "today": "اليوم",
        "last_7": "آخر 7 أيام",
        "last_30": "آخر 30 يومًا",
        "this_month": "هذا الشهر",
        "sync_now": "مزامنة الآن",
        "syncing": "جارٍ المزامنة…",
        "last_synced": "آخر مزامنة",
        "min_ago": "قبل {n} دقيقة",
        "just_now": "قبل أقل من دقيقة",
        "never_synced": "لم تتم المزامنة بعد — بانتظار المجمّع",
        "stale": "قديمة",
        "last_error": "آخر خطأ",
        "synced_ok": "تمت المزامنة مع الجهاز الآن.",
        "sync_queued": "تم طلب المزامنة — سيسحب المجمّع البيانات من الجهاز خلال ثوانٍ.",
        "sync_failed": "فشلت المزامنة",
        "total_employees": "إجمالي الموظفين",
        "present": "حاضر",
        "absent": "غائب",
        "total_hours": "إجمالي الساعات",
        "currently_in": "متواجدون الآن",
        "checked_in": "سجّل حضورًا",
        "not_checked_out": "لم يسجّل انصرافًا بعد",
        "no_checkout": "لا يوجد تسجيل انصراف",
        "checked_out": "سجّل انصرافًا",
        "live": "مباشر",
        "live_note": "تُحسب الساعات المباشرة من أول بصمة حتى الآن للموظفين الذين لم يسجّلوا انصرافًا. التقارير تعتمد على أزواج الحضور/الانصراف المكتملة فقط.",
        "employee": "الموظف",
        "id": "الرقم",
        "department": "القسم",
        "all_departments": "كل الأقسام",
        "no_department": "بدون قسم",
        "first_in": "أول دخول",
        "last_out": "آخر خروج",
        "hours": "الساعات",
        "status": "الحالة",
        "target": "الهدف",
        "target_hours": "هدف {h} ساعات",
        "target_met": "تحقق الهدف",
        "below_target": "أقل من الهدف",
        "late": "متأخر",
        "early": "مبكر",
        "late_after": "متأخر بعد {t}",
        "early_before": "مبكر قبل {t}",
        "turn_at": "نقطة التحول {t}",
        "search_placeholder": "ابحث بالاسم أو الرقم أو القسم…",
        "no_results": "لا يوجد موظفون مطابقون لبحثك.",
        "no_one_attended": "لم يحضر أحد في هذه الفترة.",
        "everyone_attended": "الجميع حضروا.",
        "no_employees": "لا يوجد موظفون في قاعدة البيانات بعد. سيضيفهم المجمّع عند أول سحب.",
        "date_range": "الفترة",
        "days": "أيام",
        "employees": "الموظفون",
        "avg_present_day": "متوسط الحضور اليومي",
        "attendance_summary": "ملخص الحضور",
        "attendance": "نسبة الحضور",
        "days_present": "أيام الحضور",
        "days_absent": "أيام الغياب",
        "avg_hrs_day": "متوسط الساعات/يوم",
        "rate": "النسبة",
        "by_department": "حسب القسم",
        "presence_per_day": "الحضور اليومي",
        "shift_window": "فترة الوردية",
        "next_day": "اليوم التالي",
        "shift_days_each": "أيام عمل (كل يوم من {a} إلى {b} في اليوم التالي)",
        "settings": "الإعدادات",
        "departments": "الأقسام",
        "add_department": "إضافة قسم",
        "department_name": "اسم القسم",
        "add": "إضافة",
        "rename": "إعادة تسمية",
        "save": "حفظ",
        "delete": "حذف",
        "confirm_delete_dept": "حذف هذا القسم؟ سيبقى موظفوه بدون قسم.",
        "no_departments": "لا توجد أقسام بعد. أضف قسمًا أعلاه ثم عيّن الموظفين أدناه.",
        "members": "أعضاء",
        "device_name": "الاسم في الجهاز",
        "display_name": "الاسم المعروض",
        "display_name_hint": "اختياري — يستبدل اسم الجهاز في هذا النظام فقط.",
        "active": "نشط",
        "inactive": "غير نشط",
        "show_inactive": "إظهار الموظفين غير النشطين",
        "saved": "تم الحفظ.",
        "deleted": "تم الحذف.",
        "error": "خطأ",
        "back_dashboard": "العودة إلى لوحة التحكم",
        "employee_profile": "ملف الموظف",
        "view_profile": "عرض الملف",
        "date": "التاريخ",
        "day": "اليوم",
        "punches": "البصمات",
        "daily_report": "تقرير الحضور اليومي",
        "range_report": "ملخص الحضور",
        "attendance_report": "تقرير الحضور",
        "daily_record": "سجل الحضور اليومي",
        "n_day_summary": "ملخص الحضور لمدة {n} يومًا",
        "attendance_by_employee": "الحضور حسب الموظف",
        "print_preview": "معاينة الطباعة",
        "page": "صفحة",
        "of": "من",
        "dark_mode": "الوضع الداكن",
        "light_mode": "الوضع الفاتح",
        "switch_lang": "English",
        "loaded": "تم التحميل",
        "device": "الجهاز",
        "weekday": "اليوم",
        "open": "مفتوح",
        "no_data": "لا توجد بيانات لهذه الفترة.",
        "employee_not_found": "الموظف غير موجود.",
        "history": "السجل",
        "profile_hint": "كل أيام العمل في الفترة، بما فيها أيام الغياب.",
        "dept_exists": "يوجد قسم بهذا الاسم مسبقًا.",
        "name_required": "الاسم مطلوب.",
    },
}

WEEKDAYS = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "ar": ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"],
}
WEEKDAYS_SHORT = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "ar": ["اثنين", "ثلاثاء", "أربعاء", "خميس", "جمعة", "سبت", "أحد"],
}
MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
    "ar": ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"],
}
MONTHS_SHORT = {
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "ar": MONTHS["ar"],
}


def normalize_lang(value: str | None) -> str | None:
    v = (value or "").strip().lower()[:2]
    return v if v in LANGS else None


def resolve_lang(query_lang: str | None, cookie_lang: str | None, accept_language: str | None, default: str) -> str:
    for candidate in (query_lang, cookie_lang):
        lang = normalize_lang(candidate)
        if lang:
            return lang
    for part in (accept_language or "").split(","):
        lang = normalize_lang(part.split(";")[0])
        if lang:
            return lang
    return default if default in LANGS else "en"


def t(lang: str, key: str, **kwargs) -> str:
    table = STRINGS.get(lang) or STRINGS["en"]
    text = table.get(key) or STRINGS["en"].get(key) or key
    return text.format(**kwargs) if kwargs else text


def is_rtl(lang: str) -> bool:
    return lang in RTL


def fmt_date(d: date | datetime, lang: str, style: str = "long") -> str:
    """long: 'Thursday, September 10, 2026' | medium: 'Sep 10, 2026' | short: 'Thu, Sep 10' | day: 'Sep 10'."""
    if isinstance(d, datetime):
        d = d.date()
    wd, wds = WEEKDAYS[lang][d.weekday()], WEEKDAYS_SHORT[lang][d.weekday()]
    mo, mos = MONTHS[lang][d.month - 1], MONTHS_SHORT[lang][d.month - 1]
    if lang == "ar":
        if style == "long":
            return f"{wd}، {d.day} {mo} {d.year}"
        if style == "medium":
            return f"{d.day} {mo} {d.year}"
        if style == "short":
            return f"{wds}، {d.day} {mos}"
        return f"{d.day} {mos}"
    if style == "long":
        return f"{wd}, {mo} {d.day:02d}, {d.year}"
    if style == "medium":
        return f"{mos} {d.day:02d}, {d.year}"
    if style == "short":
        return f"{wds}, {mos} {d.day:02d}"
    return f"{mos} {d.day:02d}"
