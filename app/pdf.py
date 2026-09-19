"""ReportLab PDF output - daily report plus a range variant.

Optional Department column, English/Arabic labels, and proper Arabic shaping
(arabic-reshaper + python-bidi) with a Unicode font when one is available.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.i18n import fmt_date, t
from app.rules import DailyReport, RangeReport, format_hm

log = logging.getLogger("app.pdf")

_HEADER_DARK = colors.HexColor("#2C3E50")
_HEADER_MID = colors.HexColor("#34495E")
_HEADER_RED = colors.HexColor("#C0392B")
_ZEBRA = colors.HexColor("#F4F6F7")
_ZEBRA_RED = colors.HexColor("#FADBD8")

_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")

# Candidate Unicode fonts (Arabic-capable). First one found wins; Helvetica otherwise.
_FONT_CANDIDATES = [
    ("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("NotoSansArabic", "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf"),
    ("Arial", "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("Tahoma", "C:/Windows/Fonts/tahoma.ttf", "C:/Windows/Fonts/tahomabd.ttf"),
]
_fonts: tuple[str, str] | None = None


def _unicode_fonts() -> tuple[str, str]:
    """(regular, bold) font names; registers a TTF pair on first use."""
    global _fonts
    if _fonts is not None:
        return _fonts
    for name, regular, bold in _FONT_CANDIDATES:
        if Path(regular).exists():
            try:
                pdfmetrics.registerFont(TTFont(name, regular))
                bold_name = name
                if Path(bold).exists():
                    bold_name = name + "-Bold"
                    pdfmetrics.registerFont(TTFont(bold_name, bold))
                _fonts = (name, bold_name)
                return _fonts
            except Exception as exc:  # pragma: no cover - font file unreadable
                log.warning("could not register font", extra={"ctx_font": regular, "ctx_error": str(exc)})
    log.warning("no Unicode TTF font found; Arabic text in PDFs will not render (install fonts-dejavu-core)")
    _fonts = ("Helvetica", "Helvetica-Bold")
    return _fonts


def _shape(text: str) -> str:
    """Reshape + reorder Arabic runs so ReportLab draws them correctly."""
    if not text or not _ARABIC_RE.search(text):
        return text
    try:
        import arabic_reshaper

        try:
            from bidi.algorithm import get_display
        except ImportError:  # python-bidi >= 0.6
            from bidi import get_display  # type: ignore

        return get_display(arabic_reshaper.reshape(text))
    except Exception:  # pragma: no cover - libraries missing
        return text


def _styles(lang: str):
    regular, bold = _unicode_fonts()
    styles = getSampleStyleSheet()
    return {
        "base": styles,
        "normal": ParagraphStyle("N", parent=styles["Normal"], fontName=regular),
        "title": ParagraphStyle("Title", parent=styles["Heading1"], fontName=bold, fontSize=18, alignment=1, spaceAfter=2),
        "subtitle": ParagraphStyle(
            "Subtitle", parent=styles["Heading3"], fontName=regular, fontSize=11, alignment=1,
            textColor=colors.HexColor("#555555"),
        ),
        "window": ParagraphStyle(
            "Window", parent=styles["Normal"], fontName=regular, fontSize=9, alignment=1,
            textColor=colors.HexColor("#888888"),
        ),
        "section": ParagraphStyle(
            "Section", parent=styles["Heading2"], fontName=bold, fontSize=12, spaceBefore=4, spaceAfter=4,
            alignment=2 if lang == "ar" else 0,
        ),
        "fonts": (regular, bold),
    }


def _doc(output_path: str, title: str) -> SimpleDocTemplate:
    return SimpleDocTemplate(
        output_path,
        pagesize=A4,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        title=title,
    )


def _table(rows: list[list[str]], col_widths: list[float], header_bg, zebra, fonts, rtl: bool, font_size=9,
           center_from_col=1, pad=4, repeat=1) -> Table:
    regular, bold = fonts
    rows = [[_shape(str(c)) for c in r] for r in rows]
    if rtl:  # mirror columns so the first column sits on the right
        rows = [list(reversed(r)) for r in rows]
        col_widths = list(reversed(col_widths))
    t_ = Table(rows, colWidths=col_widths, repeatRows=repeat)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), header_bg),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("FONTNAME", (0, 0), (-1, 0), bold),
        ("FONTNAME", (0, 1), (-1, -1), regular),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("GRID", (0, 0), (-1, -1), 0.25 if repeat else 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), pad),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
    ]
    if zebra is not None:
        style.append(("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, zebra]))
    if center_from_col is None:
        style.append(("ALIGN", (0, 0), (-1, -1), "CENTER"))
    else:
        # numeric/ID columns centered; the name column stays start-aligned
        if rtl:
            style.append(("ALIGN", (0, 1), (-2, -1), "CENTER"))
            style.append(("ALIGN", (-1, 1), (-1, -1), "RIGHT"))
        else:
            style.append(("ALIGN", (center_from_col, 1), (-1, -1), "CENTER"))
    t_.setStyle(TableStyle(style))
    return t_


def _logo_flowables(logo: bytes | None) -> list:
    """Centered logo (max 60 x 18 mm) when the admin uploaded one; silently skipped if unreadable."""
    if not logo:
        return []
    try:
        from PIL import Image as PILImage

        # Decode fully now (ReportLab reads lazily at build time) and hand over a clean PNG,
        # which also covers WEBP / GIF uploads.
        with PILImage.open(io.BytesIO(logo)) as src:
            src.load()
            clean = io.BytesIO()
            src.convert("RGBA").save(clean, format="PNG")
        clean.seek(0)
        width, height = ImageReader(clean).getSize()
        clean.seek(0)
        scale = min((60 * mm) / width, (18 * mm) / height)
        img = Image(clean, width=width * scale, height=height * scale)
        img.hAlign = "CENTER"
        return [img, Spacer(1, 3 * mm)]
    except Exception as exc:  # unsupported / corrupt image must never break the report
        log.warning("could not place the logo on the PDF", extra={"ctx_error": str(exc)})
        return []


def _window_paragraph(day_start_hour: int, style, lang: str) -> Paragraph | None:
    if not day_start_hour:
        return None
    end_hour = (day_start_hour - 1) % 24
    txt = f"{t(lang, 'shift_window')}: {day_start_hour:02d}:00 \u2192 {t(lang, 'next_day')} {end_hour:02d}:59"
    return Paragraph(_shape(txt), style)


def _has_departments(rows) -> bool:
    return any(getattr(e, "department", None) for e in rows)


def build_daily_pdf(output_path: str, company: str, report: DailyReport, lang: str = "en",
                    logo: bytes | None = None) -> None:
    st = _styles(lang)
    rtl = lang == "ar"
    target_date: date = report.target_date
    present, absent = report.present, report.absent
    with_dept = _has_departments(report.employees)

    story = _logo_flowables(logo) + [
        Paragraph(_shape(company), st["title"]),
        Paragraph(_shape(f"{t(lang, 'daily_report')} \u2014 {fmt_date(target_date, lang, 'long')}"), st["subtitle"]),
    ]
    window = _window_paragraph(report.day_start_hour, st["window"], lang)
    if window:
        story.append(window)
    story.append(Spacer(1, 8 * mm))

    story.append(
        _table(
            [
                [t(lang, "total_employees"), t(lang, "present"), t(lang, "absent"), t(lang, "total_hours")],
                [str(len(report.employees)), str(len(present)), str(len(absent)), format_hm(report.total_hours)],
            ],
            [42 * mm] * 4, _HEADER_DARK, None, st["fonts"], rtl, font_size=11, center_from_col=None, pad=7, repeat=0,
        )
    )
    story.append(Spacer(1, 8 * mm))

    story.append(Paragraph(_shape(f"{t(lang, 'present')} ({len(present)})"), st["section"]))
    if present:
        header = [t(lang, "employee"), t(lang, "id")] + ([t(lang, "department")] if with_dept else []) + [
            t(lang, "first_in"), t(lang, "last_out"), t(lang, "hours")]
        rows = [header]
        for e in present:
            rows.append(
                [e.name, e.user_id] + ([e.department or "\u2014"] if with_dept else []) + [
                    e.first_in.strftime("%H:%M:%S") if e.first_in else "\u2014",
                    e.last_out.strftime("%H:%M:%S") if e.last_out else "\u2014",
                    format_hm(e.hours_worked) if e.hours_worked else "\u2014",
                ]
            )
        widths = ([50 * mm, 18 * mm, 34 * mm] if with_dept else [65 * mm, 25 * mm]) + [22 * mm, 22 * mm, 22 * mm]
        if not with_dept:
            widths = [65 * mm, 25 * mm, 25 * mm, 25 * mm, 25 * mm]
        story.append(_table(rows, widths, _HEADER_MID, _ZEBRA, st["fonts"], rtl))
    else:
        story.append(Paragraph(_shape(f"<i>{t(lang, 'no_one_attended')}</i>"), st["normal"]))
    story.append(Spacer(1, 8 * mm))

    story.append(Paragraph(_shape(f"{t(lang, 'absent')} ({len(absent)})"), st["section"]))
    if absent:
        header = [t(lang, "employee"), t(lang, "id")] + ([t(lang, "department")] if with_dept else [])
        rows = [header] + [[e.name, e.user_id] + ([e.department or "\u2014"] if with_dept else []) for e in absent]
        widths = [85 * mm, 30 * mm, 50 * mm] if with_dept else [115 * mm, 50 * mm]
        story.append(_table(rows, widths, _HEADER_RED, _ZEBRA_RED, st["fonts"], rtl))
    else:
        story.append(Paragraph(_shape(f"<i>{t(lang, 'everyone_attended')}</i>"), st["normal"]))

    # Scheduled days off / online days are listed separately; they are not absences.
    for key, group in (("online_day", report.online), ("vacation", report.vacation), ("day_off", report.off)):
        if not group:
            continue
        story.append(Spacer(1, 8 * mm))
        story.append(Paragraph(_shape(f"{t(lang, key)} ({len(group)})"), st["section"]))
        header = [t(lang, "employee"), t(lang, "id")] + ([t(lang, "department")] if with_dept else [])
        rows = [header] + [[e.name, e.user_id] + ([e.department or "\u2014"] if with_dept else []) for e in group]
        widths = [85 * mm, 30 * mm, 50 * mm] if with_dept else [115 * mm, 50 * mm]
        story.append(_table(rows, widths, _HEADER_MID, _ZEBRA, st["fonts"], rtl))

    _doc(output_path, f"Attendance {target_date.isoformat()}").build(story)


def build_range_pdf(output_path: str, company: str, report: RangeReport, lang: str = "en",
                    logo: bytes | None = None) -> None:
    st = _styles(lang)
    rtl = lang == "ar"
    f, to = report.from_date, report.to_date
    with_dept = _has_departments(report.employees)

    story = _logo_flowables(logo) + [
        Paragraph(_shape(company), st["title"]),
        Paragraph(
            _shape(
                f"{t(lang, 'range_report')} \u2014 {fmt_date(f, lang, 'medium')} \u2013 {fmt_date(to, lang, 'medium')} "
                f"({report.days_total} {t(lang, 'days')})"
            ),
            st["subtitle"],
        ),
    ]
    window = _window_paragraph(report.day_start_hour, st["window"], lang)
    if window:
        story.append(window)
    story.append(Spacer(1, 8 * mm))

    story.append(
        _table(
            [
                [t(lang, "days"), t(lang, "employees"), t(lang, "avg_present_day"), t(lang, "total_hours")],
                [str(report.days_total), str(len(report.employees)), f"{report.avg_present_per_day:.1f}",
                 format_hm(report.total_hours)],
            ],
            [42 * mm] * 4, _HEADER_DARK, None, st["fonts"], rtl, font_size=11, center_from_col=None, pad=7, repeat=0,
        )
    )
    story.append(Spacer(1, 8 * mm))

    story.append(Paragraph(_shape(f"{t(lang, 'attendance_by_employee')} ({len(report.employees)})"), st["section"]))
    if report.employees:
        header = [t(lang, "employee"), t(lang, "id")] + ([t(lang, "department")] if with_dept else []) + [
            t(lang, "present"), t(lang, "absent"), t(lang, "rate"), t(lang, "total_hours"), t(lang, "avg_hrs_day")]
        rows = [header]
        for e in report.employees:
            rows.append(
                [e.name, e.user_id] + ([e.department or "\u2014"] if with_dept else []) + [
                    f"{e.days_present} / {e.days_expected}",
                    str(e.days_absent),
                    f"{int(e.attendance_rate * 100)}%",
                    format_hm(e.total_hours),
                    format_hm(e.avg_hours_per_attended_day) if e.days_present else "\u2014",
                ]
            )
        widths = ([40 * mm, 14 * mm, 28 * mm] if with_dept else [52 * mm, 18 * mm]) + [
            20 * mm, 16 * mm, 16 * mm, 20 * mm, 22 * mm]
        if not with_dept:
            widths = [52 * mm, 18 * mm, 22 * mm, 18 * mm, 18 * mm, 20 * mm, 24 * mm]
        story.append(_table(rows, widths, _HEADER_MID, _ZEBRA, st["fonts"], rtl))
    else:
        story.append(Paragraph(_shape(f"<i>{t(lang, 'no_employees')}</i>"), st["normal"]))

    _doc(output_path, f"Attendance {f.isoformat()} to {to.isoformat()}").build(story)
