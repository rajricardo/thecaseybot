"""
Pure strike/expiry ranking logic, ported from
../IBKRStocks/ibkr_trading_lib.py's select_contract_params — kept
dependency-free (no ib_async import) so it's unit-testable without a live
IBKR connection. The live underlying-price/option-chain lookup that feeds
these lives in ibkr_client.py.
"""

import re
from datetime import datetime

STRIKE_SEL_RE = re.compile(r"^(\d+)(ITM|OTM)$")


def parse_strike_selection(strike_selection):
    """'<rank>OTM' / '<rank>ITM' (e.g. '1OTM', '2ITM') -> (rank, status).
    Defaults to (1, 'OTM') on anything unrecognized."""
    m = STRIKE_SEL_RE.match((strike_selection or "1OTM").upper())
    if m is None:
        return 1, "OTM"
    return int(m.group(1)), m.group(2)


def is_standard_strike(strike):
    """Most equity options list on whole- or half-dollar increments; IBKR's
    reqSecDefOptParams occasionally returns a stray non-standard value
    (e.g. 609.78) — filter those out before ranking."""
    return strike == round(strike) or (strike * 2) == round(strike * 2)


def rank_strike_pool(price, strikes, right, strike_selection):
    """Ranked candidate strikes, preferred-first, for the given
    <rank>OTM/<rank>ITM selection. right: 'C' or 'P'. Returns the pool
    starting at the requested rank (a list, not a single value) so the
    caller can fall back to the next candidate if the preferred strike has
    no contract at IBKR for the target expiry. Returns [] if there are no
    strikes on the requested side."""
    rank, status = parse_strike_selection(strike_selection)
    standard = [s for s in strikes if is_standard_strike(s)]

    if status == "OTM":
        pool = sorted(s for s in standard if s > price) if right == "C" \
            else sorted((s for s in standard if s < price), reverse=True)
    else:  # ITM
        pool = sorted((s for s in standard if s < price), reverse=True) if right == "C" \
            else sorted(s for s in standard if s > price)

    if not pool:
        return []
    start_idx = min(rank - 1, len(pool) - 1)
    return pool[start_idx:]


def pick_expiry(expirations, today, expiry_selection="nearest"):
    """expirations: iterable of 'YYYYMMDD' strings. today: a date. Returns
    the target expiry string, or None if nothing is on/after today."""
    future = sorted(
        e for e in expirations
        if datetime.strptime(e, "%Y%m%d").date() >= today
    )
    if not future:
        return None

    if (expiry_selection or "nearest").lower() == "weeklies":
        # Strictly after today, not >= — otherwise running this on a Friday
        # would pick today's own (0DTE) Friday expiry instead of next week's.
        fridays = [e for e in future
                   if datetime.strptime(e, "%Y%m%d").weekday() == 4
                   and datetime.strptime(e, "%Y%m%d").date() > today]
        return fridays[0] if fridays else future[0]

    return future[0]
