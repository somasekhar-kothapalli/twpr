"""Helpers shared by both calendar scrapers, so their rows come out identical."""
import datetime

IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
DATE_FORMAT = "%d-%m-%Y"  # default for every date the scrapers take or return


def parse_date(value):
    """'23-09-2026' (default) or ISO '2026-09-23' or a date -> date; None if unparseable."""
    if isinstance(value, datetime.date):
        return value
    for fmt in (DATE_FORMAT, "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None


def format_date(value):
    """Any parse_date-able value -> DATE_FORMAT string; None if unparseable."""
    parsed = parse_date(value)
    return parsed.strftime(DATE_FORMAT) if parsed else None


def gmt_to_ist(time_str):
    """'04:00 PM' / '16:00' (GMT) -> '09:30 PM' (IST). Non-clock text ('All Day',
    blank) is kept as-is rather than dropping the row; blank becomes None."""
    time_str = time_str.strip()
    if not time_str:
        return None
    for fmt in ("%I:%M %p", "%H:%M"):
        try:
            parsed = datetime.datetime.strptime(time_str, fmt)
        except ValueError:
            continue
        return (parsed + IST_OFFSET).strftime("%I:%M %p")
    return time_str


def to_mb_suffixed(text):
    """'-1.6M' / '250K' -> million barrels ('53Bcf' / '53.00B' -> 53.0 Bcf, unit unchanged); None if blank or unparseable.
    A trailing '%' (refinery utilisation) is stripped and the number kept as-is."""
    text = text.strip().replace(",", "").rstrip("%")
    if not text:
        return None
    mult = 1.0
    if text.endswith("Bcf"):      # natural gas storage: '53Bcf' (tradingeconomics)
        text = text[:-3]
    elif text.endswith("B"):      # '53.00B' (investing.com); billion cubic feet, kept as-is
        text = text[:-1]
    elif text.endswith("M"):
        text = text[:-1]
    elif text.endswith("K"):
        text = text[:-1]
        mult = 0.001
    try:
        return float(text) * mult
    except ValueError:
        return None


def latest_released_row(rows):
    """Most recently released row (actual present), or None."""
    released = [r for r in rows or [] if r["actual"] is not None and parse_date(r["release_date"])]
    return max(released, key=lambda r: parse_date(r["release_date"])) if released else None


def pending_row(rows):
    """The next upcoming release: the earliest-dated row whose actual hasn't
    printed yet. None if every row is released."""
    pending = [r for r in rows or [] if r["actual"] is None and parse_date(r["release_date"])]
    return min(pending, key=lambda r: parse_date(r["release_date"])) if pending else None


def latest_due_row(rows, today):
    """The most recent release dated on or before `today` (a date), released or
    not. Unlike pending_row it stays on the current release after it prints
    instead of jumping to next week's."""
    due = [r for r in rows or [] if parse_date(r["release_date"]) and parse_date(r["release_date"]) <= today]
    return max(due, key=lambda r: parse_date(r["release_date"])) if due else None


def current_release(rows, today, max_age_days=1):
    """The latest release due by `today`, provided it is at most `max_age_days` old.

    Guards the holiday-delayed week: if today's report is not on the calendar
    yet, the latest due row is LAST week's already-printed one, which must not be
    mistaken for a fresh release. Raises RuntimeError instead of guessing.
    """
    row = latest_due_row(rows, today)
    if row is None:
        raise RuntimeError(f"no release due by {today:%d-%m-%Y}")
    age = (today - parse_date(row["release_date"])).days
    if age > max_age_days:
        raise RuntimeError(
            f"latest release {row['release_date']} is {age} days old - the current report is not on the calendar yet")
    return row


def row_for_release(rows, release_date=None):
    """The calendar row released on `release_date` (DD-MM-YYYY by default; ISO or
    a date also accepted), or None. With no date, the latest released row. Exact
    match: no week mapping."""
    if release_date is None:
        return latest_released_row(rows)
    wanted = format_date(release_date)
    return next((r for r in rows or [] if wanted and r["release_date"] == wanted), None)
