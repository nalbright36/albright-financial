"""Quick add: one line of plain English -> a new item's fields.

  "Pay rent every month on the 1st"  -> "Pay rent", monthly on day 1, from the next 1st
  "Gym Mon Wed Fri 6am"               -> "Gym", every Mon, Wed, Fri at 6:00 AM
  "Call dentist tomorrow 3pm"         -> "Call dentist", tomorrow at 3:00 PM

Understood: today / tomorrow / tonight, (on|next) <weekday>, in N days|weeks,
"Dec 31", "December 31st", 12/31 and 12/31/2026; times like 6am, 3:30pm,
15:00, noon, "at 3", and ranges (3-4pm, 3pm to 4pm); repeats: every day /
daily, every weekday / weekdays, every <weekday(s)> or 2+ weekdays in a row,
every week, every other week, every N weeks, every month (on the 1st),
monthly, on the 2nd Tuesday of every month, every year / yearly / annually;
"until <date>" and "N times". Whatever isn't understood stays in the title.
Quick-added items are tasks (an event can be picked in the full form)."""
import calendar
import re
from datetime import date, time, timedelta

from . import recurrence

DAYS = {name: i for i, names in enumerate([("mon", "monday", "mo"), ("tue", "tues", "tuesday", "tu"),
                                           ("wed", "weds", "wednesday", "we"), ("thu", "thur", "thurs", "thursday", "th"),
                                           ("fri", "friday", "fr"), ("sat", "saturday", "sa"), ("sun", "sunday", "su")])
        for name in names}
DAY_WORD = r"(?:mon(?:day)?|tue(?:s|sday)?|wed(?:s|nesday)?|thu(?:r|rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})
MONTH_WORD = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
ORDINAL_WORDS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "last": -1}
TIME = r"(?:(?P<{p}h>\d{{1,2}})(?::(?P<{p}m>\d{{2}}))?\s*(?P<{p}ap>am|pm|a\.m\.|p\.m\.)?|(?P<{p}word>noon|midnight))"


def _day_index(word):
    return DAYS.get(word.lower().rstrip("."))


def _month_index(word):
    word = word.lower()
    return MONTHS.get(word) or MONTHS.get(word[:3])


def _next_weekday(start, weekday, skip_today=False):
    ahead = (weekday - start.weekday()) % 7
    if ahead == 0 and skip_today:
        ahead = 7
    return start + timedelta(days=ahead)


def _clock(h, m, ap, word, default_pm=False):
    if word:
        return time(12, 0) if word.lower() == "noon" else time(0, 0)
    hour, minute = int(h), int(m or 0)
    if hour > 23 or minute > 59:
        return None
    ap = (ap or "").replace(".", "").lower()
    if ap == "pm" and hour < 12:
        hour += 12
    elif ap == "am" and hour == 12:
        hour = 0
    elif not ap and default_pm and 1 <= hour <= 6:
        hour += 12                                     # "at 3" means 3 PM
    return time(hour, minute)


class _Text:
    """The words still unclaimed - each recognized phrase is cut out, the rest becomes the title."""

    def __init__(self, text):
        self.text = " " + re.sub(r"\s+", " ", text.strip()) + " "

    def take(self, pattern, valid=None):
        """Cut out the first match of `pattern` (only if valid(match), when given) and return it."""
        match = re.search(pattern, self.text, re.I)
        if match and (valid is None or valid(match)):
            self.text = self.text[:match.start()] + " " + self.text[match.end():]
            return match
        return None


def _date_from(match, today):
    """A matched "Dec 31" / "12/31/2026" -> a date (this year, or next if it's passed)."""
    g = match.groupdict()
    if g.get("mname"):
        month, day, year = _month_index(g["mname"]), int(g["mday"]), g.get("myear")
    else:
        month, day, year = int(g["num_m"]), int(g["num_d"]), g.get("num_y")
    try:
        if year:
            year = int(year)
            return date(year + 2000 if year < 100 else year, month, day)
        found = date(today.year, month, day)
        return found if found >= today else date(today.year + 1, month, day)
    except (TypeError, ValueError):
        return None


DATE_BODY = (rf"(?:(?P<mname>{MONTH_WORD})\.?\s+(?P<mday>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<myear>\d{{4}}))?"
             r"|(?P<num_m>\d{1,2})/(?P<num_d>\d{1,2})(?:/(?P<num_y>\d{2,4}))?)\b")
DATE_PATTERN = r"\b(?:on\s+)?" + DATE_BODY


def parse(text, today):
    """{"title", "date", "start_time", "end_time", "recurrence", "recurrence_until", "recurrence_count", "kind"}."""
    t = _Text(text or "")
    out = {"date": None, "start_time": None, "end_time": None, "recurrence": "", "recurrence_until": None,
           "recurrence_count": None, "kind": "task"}
    repeat = {}

    # --- how long it repeats ---
    m = t.take(r"\buntil\s+" + DATE_BODY)
    if m:
        out["recurrence_until"] = _date_from(m, today)
    m = t.take(r"\b(?:for\s+)?(\d{1,3})\s+times\b")
    if m:
        out["recurrence_count"] = int(m.group(1))

    # --- repeats ---
    if t.take(r"\b(?:every\s*day|daily|each\s+day)\b"):
        repeat = {"repeat": "daily"}
    elif t.take(r"\b(?:every\s+weekday|weekdays|each\s+weekday|mon(?:day)?\s*-\s*fri(?:day)?)\b"):
        repeat = {"repeat": "weekdays"}
    if not repeat:
        m = t.take(rf"\b(?:on\s+)?(?:the\s+)?(?P<nth>first|second|third|fourth|last|1st|2nd|3rd|4th)\s+(?P<wd>{DAY_WORD})"
                   rf"\s+(?:of\s+)?(?:every|each|the)\s+month\b")
        if m:
            repeat = {"repeat": "monthly_nth", "nth": ORDINAL_WORDS[m.group("nth").lower()],
                      "nth_weekday": recurrence.WEEKDAYS[_day_index(m.group("wd"))]}
    if not repeat:
        m = t.take(r"\b(?:every|each)\s+month(?:ly)?(?:\s+on\s+the\s+(\d{1,2})(?:st|nd|rd|th)?)?\b") or \
            t.take(r"\bmonthly(?:\s+on\s+the\s+(\d{1,2})(?:st|nd|rd|th)?)?\b") or \
            t.take(r"\bon\s+the\s+(\d{1,2})(?:st|nd|rd|th)\s+of\s+(?:every|each)\s+month\b")
        if m:
            repeat = {"repeat": "monthly_day", "month_day": int(m.group(1)) if m.group(1) else None}
    if not repeat:
        m = t.take(r"\b(?:every|each)\s+(other|\d{1,2})\s+weeks?(?:\s+on)?\b")
        if m:
            repeat = {"repeat": "every_n_weeks", "interval": 2 if m.group(1).lower() == "other" else int(m.group(1))}
        elif t.take(r"\b(?:every|each)\s+week(?:\s+on)?\b|\bweekly(?:\s+on)?\b"):
            repeat = {"repeat": "weekly"}
    if not repeat and t.take(r"\b(?:every|each)\s+year\b|\byearly\b|\bannually\b"):
        repeat = {"repeat": "yearly"}
    # Weekday names: after "every" (or 2+ in a row) they're the repeat days; one alone is a date.
    days_run = rf"(?:{DAY_WORD}\.?(?:\s*(?:,|and|&)\s*|\s+))*{DAY_WORD}\.?"
    m = t.take(rf"\b(?:every|each|on)\s+(?P<days>{days_run})(?=\s|$)") if repeat.get("repeat") in (None, "weekly", "every_n_weeks") else None
    if m is None and repeat.get("repeat") in (None, "weekly", "every_n_weeks"):
        found = re.search(rf"\b(?P<days>{days_run})(?=\s|$)", t.text, re.I)
        names = re.findall(DAY_WORD, found.group("days"), re.I) if found else []
        if len(names) >= 2 or (names and repeat):
            m = t.take(re.escape(found.group(0)))
    if m is not None:
        names = re.findall(DAY_WORD, m.group("days") if "days" in m.groupdict() else m.group(0), re.I)
        days = [recurrence.WEEKDAYS[_day_index(n)] for n in names]
        every = re.match(r"\s*(?:every|each)\b", m.group(0), re.I) or len(days) > 1 or repeat
        if every:
            repeat = {**repeat, "repeat": repeat.get("repeat") or "weekly", "days": days}
        else:
            out["date"] = _next_weekday(today, recurrence.WEEKDAYS.index(days[0]))

    # --- the day ---
    if out["date"] is None:
        if t.take(r"\btoday\b|\btonight\b|\bthis\s+evening\b"):
            out["date"] = today
        elif t.take(r"\btomorrow\b|\btmrw\b"):
            out["date"] = today + timedelta(days=1)
        else:
            m = t.take(rf"\bnext\s+({DAY_WORD})\b")
            if m:
                out["date"] = _next_weekday(today, _day_index(m.group(1)), skip_today=True)
            else:
                m = t.take(r"\bin\s+(\d{1,3})\s+(day|week)s?\b")
                if m:
                    out["date"] = today + timedelta(days=int(m.group(1)) * (7 if m.group(2).lower() == "week" else 1))
                else:
                    m = t.take(DATE_PATTERN)
                    if m:
                        out["date"] = _date_from(m, today)
                    else:
                        m = t.take(rf"\b(?:on\s+|this\s+)?({DAY_WORD})\b")          # "friday": the next one (or today)
                        if m:
                            out["date"] = _next_weekday(today, _day_index(m.group(1)))

    # --- the time ---
    start, end = TIME.format(p="s"), TIME.format(p="e")
    # A range needs an am/pm, minutes or noon somewhere ("3-4" alone could be anything); a single time needs one
    # of those or a leading "at" ("Buy 3 apples" has no time in it).
    m = t.take(rf"\b(?:from\s+)?{start}\s*(?:-|–|to|until)\s*{end}(?=\s|$)",
               valid=lambda m: any(m.group(k) for k in ("sap", "eap", "sword", "eword", "sm", "em")))
    if m:
        g = m.groupdict()
        ap = g["sap"] or (g["eap"] if g["eap"] and not g["sword"] else None)
        out["start_time"] = _clock(g["sh"], g["sm"], ap, g["sword"])
        out["end_time"] = _clock(g["eh"], g["em"], g["eap"] or ap, g["eword"])
    else:
        m = t.take(rf"\b(?:at\s+)?{start}(?=\s|$)",
                   valid=lambda m: any(m.group(k) for k in ("sap", "sword", "sm")) or re.match(r"\s*at\b", m.group(0), re.I))
        if m:
            g = m.groupdict()
            out["start_time"] = _clock(g["sh"], g["sm"], g["sap"], g["sword"], default_pm=not g["sap"])

    # --- the repeat rule and its first day ---
    if repeat:
        first = out["date"] or today
        if repeat["repeat"] == "monthly_day" and repeat.get("month_day") and not out["date"]:
            day = min(repeat["month_day"], 28)
            first = date(today.year, today.month, day) if day >= today.day else \
                (date(today.year + (today.month == 12), today.month % 12 + 1, day))
        if repeat["repeat"] in ("weekly", "every_n_weeks", "weekdays") and not out["date"]:
            wanted = repeat.get("days") or (recurrence.WEEKDAYS[:5] if repeat["repeat"] == "weekdays" else [])
            if wanted:
                first = min(_next_weekday(today, recurrence.WEEKDAYS.index(d)) for d in wanted)
        if repeat["repeat"] == "monthly_nth" and not out["date"]:
            first = _first_nth(today, repeat["nth"], recurrence.WEEKDAYS.index(repeat["nth_weekday"]))
        out["date"] = first
        out["recurrence"] = recurrence.build(repeat["repeat"], first, days=repeat.get("days") or (),
                                             interval=repeat.get("interval", 1), month_day=repeat.get("month_day"),
                                             nth=repeat.get("nth"), nth_weekday=repeat.get("nth_weekday"))
    else:
        out["recurrence_until"] = out["recurrence_count"] = None
    if out["recurrence_until"]:
        out["recurrence_count"] = None

    out["date"] = out["date"] or today
    title = re.sub(r"\s+", " ", t.text).strip()
    title = re.sub(r"^(?:on|at|every|each|from|to)\b\s*|\s*\b(?:on|at|every|each|from|to|the)$", "", title, flags=re.I).strip(" ,-")
    out["title"] = (title[:1].upper() + title[1:])[:200] if title else "Untitled"
    return out


def _first_nth(today, nth, weekday):
    """The first `nth` `weekday` of a month on or after today."""
    year, month = today.year, today.month
    for _ in range(14):
        days = [d for d in calendar.Calendar().itermonthdates(year, month) if d.month == month and d.weekday() == weekday]
        pick = days[nth - 1] if nth > 0 and nth <= len(days) else days[-1]
        if pick >= today:
            return pick
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return today
