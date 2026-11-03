"""Quick add: "42.18 Publix groceries" -> $42.18 out, payee "Publix", category Groceries.

  - the amount is the first or last word ("$42.18", "42", "1,200.00"); a leading "+" means money in, and so
    does an Income category - anything else is money out;
  - the last word or two naming one of the account's categories (any case) is the category;
  - what's left is the payee. No category named: the scope's rules decide (money.categorize).
"""
import re

from . import money

AMOUNT = re.compile(r"^[+-]?\$?\d[\d,]*(\.\d{1,2})?$")


def parse(text, categories):
    """{"amount": Decimal, "payee": str, "category": Category or None}, or None if there's no amount."""
    words = (text or "").split()
    if not words:
        return None
    if AMOUNT.match(words[0]):
        raw, words = words[0], words[1:]
    elif AMOUNT.match(words[-1]):
        raw, words = words[-1], words[:-1]
    else:
        return None
    amount = money.to_decimal(raw.lstrip("+-"))
    if amount is None or amount == 0:
        return None
    by_name = {c.name.lower(): c for c in categories}
    category = None
    for size in (2, 1):
        if len(words) >= size and " ".join(words[-size:]).lower() in by_name:
            category = by_name[" ".join(words[-size:]).lower()]
            words = words[:-size]
            break
    money_in = raw.startswith("+") or (category is not None and category.is_income)
    return {"amount": amount if money_in else -amount, "payee": " ".join(words)[:200], "category": category}
