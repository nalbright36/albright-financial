"""Bank CSV import: read the upload in memory (size- and row-limited, plain text only - nothing in a cell is ever
evaluated), map its columns once per account (Account.csv_mapping), preview, skip duplicates, apply rules.

The file itself is never saved: its rows travel between the steps inside a signed form field (pack / unpack),
and the upload is dropped when the request ends. Uploads stay under Django's in-memory limit, so not even a
temporary file is written.

Duplicates: an existing transaction on the same account with the same date and amount and a similar description
(one contains the other, the same first word, or close spelling) - one match each, so two real $5 coffees on one
day in the file still both import if only one is already there. Hand-entered ones count.
"""
import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal
from difflib import SequenceMatcher

from django.core import signing

from . import money
from .models import Rule, Transaction

MAX_BYTES = 1_000_000
MAX_ROWS = 5000
SALT = "albright_money.import"
MAX_AGE = 60 * 60
DATE_FORMATS = [("%m/%d/%Y", "MM/DD/YYYY"), ("%m/%d/%y", "MM/DD/YY"), ("%Y-%m-%d", "YYYY-MM-DD"),
                ("%d/%m/%Y", "DD/MM/YYYY"), ("%d.%m.%Y", "DD.MM.YYYY"), ("%b %d, %Y", "Mon D, YYYY")]
FORMAT_CODES = {code for code, _label in DATE_FORMATS}
GUESS = {"date": ("date", "posted", "transaction date", "posting date"),
         "description": ("description", "payee", "merchant", "name", "memo", "details"),
         "amount": ("amount",), "debit": ("debit", "withdrawal", "withdrawals", "money out"),
         "credit": ("credit", "deposit", "deposits", "money in")}
FORMULA_START = ("=", "+", "@", "\t", "\r")


class ImportProblem(ValueError):
    """Something about the file the person can fix - the message is shown as is."""


def read(upload):
    """(headers, rows) from an uploaded CSV - text cells, blank lines dropped."""
    if upload.size > MAX_BYTES:
        raise ImportProblem(f"That file is too big - the limit is {MAX_BYTES // 1_000_000} MB.")
    raw = upload.read(MAX_BYTES + 1)
    if b"\x00" in raw[:4096]:
        raise ImportProblem("That doesn't look like a CSV file.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    try:
        rows = [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in row)]
    except csv.Error as exc:
        raise ImportProblem(f"Couldn't read that file as CSV ({exc}).") from None
    if len(rows) < 2:
        raise ImportProblem("That file has no transactions in it.")
    if len(rows) - 1 > MAX_ROWS:
        raise ImportProblem(f"That file has more than {MAX_ROWS:,} rows - split it into smaller files.")
    width = len(rows[0])
    return rows[0], [row + [""] * (width - len(row)) for row in rows[1:]]


def guess_mapping(headers):
    """A first guess at the columns from their names."""
    lowered = [h.lower().strip() for h in headers]
    found = {}
    for field, names in GUESS.items():
        for name in names:
            if name in lowered:
                found[field] = headers[lowered.index(name)]
                break
    if "amount" in found:
        found.pop("debit", None)
        found.pop("credit", None)
    found.setdefault("date_format", DATE_FORMATS[0][0])
    return found


def mapping_fits(mapping, headers):
    """Can this saved mapping read a file with these headers?"""
    if not mapping or not mapping.get("date") or not mapping.get("description"):
        return False
    needed = [mapping["date"], mapping["description"]] + (
        [mapping["amount"]] if mapping.get("amount") else [mapping.get("debit"), mapping.get("credit")])
    return all(col and col in headers for col in needed) and mapping.get("date_format") in FORMAT_CODES


def clean_text(text):
    """A cell as plain text: never something a spreadsheet would run as a formula if it's exported again."""
    text = (text or "").strip()
    while text.startswith(FORMULA_START):
        text = text[1:].strip()
    return text[:200]


def parse_amount(text):
    """"$1,234.56", "-12.00", "(12.00)" -> Decimal; "" or nonsense -> None."""
    text = (text or "").strip()
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace("$", "").replace(",", "").replace(" ", "")
    if not re.fullmatch(r"[+-]?\d+(\.\d+)?", text):
        return None
    value = money.to_decimal(text)
    return -value if negative and value is not None else value


def normalize(headers, rows, mapping):
    """Rows as [{"line", "date" (ISO), "payee", "amount" (str)}] and problems [(line, message)]."""
    col = {name: headers.index(name) for name in headers}
    out, problems = [], []
    for n, row in enumerate(rows, start=2):
        try:
            when = datetime.strptime(row[col[mapping["date"]]].strip(), mapping["date_format"]).date()
        except (ValueError, KeyError):
            problems.append((n, f"couldn't read the date “{clean_text(row[col[mapping['date']]])}”"))
            continue
        if mapping.get("amount"):
            amount = parse_amount(row[col[mapping["amount"]]])
            if amount is not None and mapping.get("negate"):
                amount = -amount
        else:
            debit, credit = parse_amount(row[col[mapping["debit"]]]), parse_amount(row[col[mapping["credit"]]])
            amount = None if debit is None and credit is None else (abs(credit or 0) - abs(debit or 0))
            amount = money.to_decimal(amount) if amount is not None else None
        if amount is None:
            problems.append((n, "no amount"))
            continue
        out.append({"line": n, "date": when.isoformat(), "payee": clean_text(row[col[mapping["description"]]]),
                    "amount": str(amount)})
    return out, problems


def pack(data):
    return signing.dumps(data, salt=SALT, compress=True)


def unpack(token):
    """The signed rows back, or ImportProblem if they were changed or are over an hour old."""
    try:
        return signing.loads(token or "", salt=SALT, max_age=MAX_AGE)
    except signing.BadSignature:
        raise ImportProblem("That import expired or was changed - upload the file again.") from None


def _words(text):
    return re.findall(r"[a-z]+", (text or "").lower())


def similar(a, b):
    """Close enough to be the same transaction's description."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return True                          # nothing to tell them apart by but the date and amount
    sa, sb = " ".join(wa), " ".join(wb)
    return sa in sb or sb in sa or wa[0] == wb[0] or SequenceMatcher(None, sa, sb).ratio() >= 0.6


def mark_duplicates(account, rows):
    """Set row["duplicate"] on rows already on the account (one existing transaction per row)."""
    dates = {r["date"] for r in rows}
    existing = {}
    for t in Transaction.objects.filter(account=account, date__in=[date.fromisoformat(d) for d in dates]):
        existing.setdefault((t.date.isoformat(), t.amount), []).append(t)
    for row in rows:
        candidates = existing.get((row["date"], Decimal(row["amount"])), [])
        match = next((t for t in candidates if similar(t.payee, row["payee"])), None)
        row["duplicate"] = match is not None
        if match is not None:
            candidates.remove(match)
    return rows


def preview(account, rows):
    """The rows marked duplicate or new, with the category a rule would give each new one."""
    rules = list(Rule.objects.filter(money.scope_filter(account.owner, account.space)).select_related("category"))
    mark_duplicates(account, rows)
    for row in rows:
        if not row["duplicate"]:
            probe = Transaction(account=account, payee=row["payee"], amount=money.to_decimal(row["amount"]))
            rule = money.rule_for(probe, rules)
            row["category"] = rule.category.name if rule else ""
    return rows


def import_rows(account, rows, user):
    """Create the new rows' transactions (duplicates skipped again, in case something was added meanwhile).
    Returns (created, skipped)."""
    rules = list(Rule.objects.filter(money.scope_filter(account.owner, account.space)).select_related("category"))
    mark_duplicates(account, rows)
    created = 0
    for row in rows:
        if row["duplicate"]:
            continue
        t = Transaction(account=account, date=date.fromisoformat(row["date"]), amount=money.to_decimal(row["amount"]),
                        payee=row["payee"], entered_by=user, source=Transaction.IMPORT)
        money.categorize(t, rules)
        t.save()
        created += 1
    return created, len(rows) - created
