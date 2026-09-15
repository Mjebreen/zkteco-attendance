"""Date parsing shared by the API and the web UI (today | yesterday | YYYY-MM-DD, plus from/to/range)."""

from __future__ import annotations

from datetime import date, datetime, timedelta


def today() -> date:
    """Local calendar date (container TZ == device TZ)."""
    return datetime.now().date()


def parse_date(s: str | None) -> date:
    """today | yesterday | YYYY-MM-DD (defaults to today when empty)."""
    s = (s or "today").strip().lower()
    if s == "today":
        return today()
    if s == "yesterday":
        return today() - timedelta(days=1)
    return datetime.strptime(s, "%Y-%m-%d").date()


def resolve_range(
    raw_date: str | None,
    raw_from: str | None,
    raw_to: str | None,
    raw_range: str | None = None,
    strict: bool = False,
) -> tuple[date, date]:
    """Turn query parameters into an inclusive (from_date, to_date).

    Precedence: range=N > from/to > date > yesterday.
    With strict=True a malformed date raises ValueError instead of falling back.
    """
    t = today()
    fallback = (t - timedelta(days=1), t - timedelta(days=1))
    try:
        if raw_range:
            try:
                n = max(1, min(int(raw_range), 365))
            except ValueError:
                n = 7
            f, to = t - timedelta(days=n - 1), t
        elif raw_from or raw_to:
            f = parse_date(raw_from) if raw_from else parse_date(raw_to)
            to = parse_date(raw_to) if raw_to else f
        elif raw_date:
            f = to = parse_date(raw_date)
        else:
            f, to = fallback
    except ValueError:
        if strict:
            raise
        f, to = fallback
    if f > to:
        f, to = to, f
    return f, to
