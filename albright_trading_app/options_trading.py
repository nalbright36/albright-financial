import datetime as dt
import re

from django.conf import settings

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest

import logging

logger = logging.getLogger(__name__)

# A hard safety rail, not a user setting — always exit before this
# many days to expiration, regardless of other risk settings, to
# avoid assignment/pin risk right at the end of a contract's life.
MINIMUM_DTE_BEFORE_FORCE_CLOSE = 2

# Standard OCC option symbol format, e.g. "AAPL260605C00315000":
# root symbol + YYMMDD + C/P + strike*1000 zero-padded to 8 digits.
_OCC_SYMBOL_PATTERN = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def parse_occ_symbol(contract_symbol):
    """
    Parses a standard OCC option symbol into its components. Returns
    None if the symbol doesn't match the expected format.
    """
    match = _OCC_SYMBOL_PATTERN.match(contract_symbol)
    if not match:
        return None

    root, date_str, cp, strike_str = match.groups()
    try:
        expiration_date = dt.datetime.strptime(date_str, "%y%m%d").date()
    except ValueError:
        return None

    return {
        "underlying": root,
        "expiration_date": expiration_date,
        "option_type": "call" if cp == "C" else "put",
        "strike": int(strike_str) / 1000.0,
    }


def get_atm_option_contract(underlying_symbol, option_type, target_dte, current_price):
    """
    Fetches the live option chain for underlying_symbol and returns
    the at-the-money contract (strike closest to current_price) of
    the given type ("call" or "put"), at the expiration closest to
    target_dte days out. Returns None if nothing suitable was found
    (no chain data, no live quote to price it with, etc.).

    Returns: {"contract_symbol", "strike", "expiration_date", "premium"}
    """
    client = OptionHistoricalDataClient(
        api_key=settings.ALPACA_API_KEY,
        secret_key=settings.ALPACA_SECRET_KEY,
    )

    try:
        chain = client.get_option_chain(
            OptionChainRequest(underlying_symbol=underlying_symbol)
        )
    except Exception:
        logger.exception("Failed to fetch option chain for %s", underlying_symbol)
        return None

    if not chain:
        return None

    target_date = dt.date.today() + dt.timedelta(days=target_dte)
    candidates = []

    for contract_symbol, snapshot in chain.items():
        parsed = parse_occ_symbol(contract_symbol)
        if parsed is None or parsed["option_type"] != option_type:
            continue

        quote = getattr(snapshot, "latest_quote", None)
        ask_price = getattr(quote, "ask_price", None) if quote else None
        if not ask_price:
            continue  # no live quote available — can't safely price this contract

        candidates.append({
            "contract_symbol": contract_symbol,
            "strike": parsed["strike"],
            "expiration_date": parsed["expiration_date"],
            "premium": float(ask_price),
        })

    if not candidates:
        return None

    expirations = sorted({c["expiration_date"] for c in candidates})
    closest_expiration = min(expirations, key=lambda d: abs((d - target_date).days))
    same_expiration = [c for c in candidates if c["expiration_date"] == closest_expiration]

    return min(same_expiration, key=lambda c: abs(c["strike"] - current_price))


def get_current_option_prices(contract_symbols):
    """
    Returns {contract_symbol: latest_price} for a list of OCC option
    contract symbols — used for live unrealized P&L on open option
    positions, same purpose as get_current_prices() for stocks.
    """
    if not contract_symbols:
        return {}

    client = OptionHistoricalDataClient(
        api_key=settings.ALPACA_API_KEY,
        secret_key=settings.ALPACA_SECRET_KEY,
    )

    prices = {}
    try:
        from alpaca.data.requests import OptionLatestQuoteRequest
        quotes = client.get_option_latest_quote(
            OptionLatestQuoteRequest(symbol_or_symbols=list(set(contract_symbols)))
        )
        for symbol, quote in quotes.items():
            if quote is not None and getattr(quote, "ask_price", None):
                prices[symbol] = float(quote.ask_price)
    except Exception:
        logger.exception("Failed to fetch current option prices")

    return prices