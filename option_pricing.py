"""
Pure limit-price resolution for MARKET | MIDPOINT | BID | ASK | LAST, ported
from ../IBKRStocks/ibkr_trading_lib.py's _limit_price_from/_round_to_tick —
same tick-rounding and midpoint-fallback behavior that's already proven out
there, kept dependency-free here (no ib_async import) so it's unit-testable
without a live IBKR connection.
"""


def is_nan(x):
    return x != x


def round_to_tick(price):
    tick = 0.05 if price < 3.00 else 0.10
    return round(round(price / tick) * tick, 2)


def limit_price_from_quote(bid, ask, last, price_type):
    """price_type: MIDPOINT | BID | ASK | LAST (MARKET has no limit price,
    handled by the caller before this is reached). Falls back to midpoint
    when the requested side is missing, and to last when both bid and ask
    are missing. Returns 0.0 if nothing usable is available."""
    bid = bid if bid and not is_nan(bid) and bid > 0 else 0.0
    ask = ask if ask and not is_nan(ask) and ask > 0 else 0.0
    last = last if last and not is_nan(last) and last > 0 else 0.0
    midpoint = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else last

    price_type = price_type.upper()
    if price_type == "BID":
        return bid if bid > 0 else midpoint
    if price_type == "ASK":
        return ask if ask > 0 else midpoint
    if price_type == "LAST":
        return last if last > 0 else midpoint
    return midpoint  # MIDPOINT
