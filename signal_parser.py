"""
Parses Casey's discord-style "taking" messages (e.g. "I'm taking QQQ 669p")
and extracts the ticker + option direction.
"""

import re

TICKER_ALIASES = {
    "spy": "SPY",
    "qqq": "QQQ",
    "iwm": "IWM",
    "tsla": "TSLA",
    "tesla": "TSLA",
    "amd": "AMD",
    "aapl": "AAPL",
    "apple": "AAPL",
    "nvda": "NVDA",
    "nvidia": "NVDA",
    "msft": "MSFT",
    "microsoft": "MSFT",
    "amzn": "AMZN",
    "amazon": "AMZN",
    "meta": "META",
    "googl": "GOOGL",
    "google": "GOOGL",
    "nflx": "NFLX",
    "netflix": "NFLX",
    "gld": "GLD",
    "mags": "MAGS",
    "slv": "SLV",
}

TAKING_RE = re.compile(r"\btaking\b", re.IGNORECASE)
STRIKE_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(calls|call|puts|put|c|p)\b", re.IGNORECASE)


def find_nearest_ticker(text, anchor_pos):
    """Return the ticker symbol whose alias mention is closest to anchor_pos, or None."""
    ticker = None
    best_distance = None
    for alias, symbol in TICKER_ALIASES.items():
        for m in re.finditer(r"\b" + re.escape(alias) + r"\b", text, re.IGNORECASE):
            distance = abs(m.start() - anchor_pos)
            if best_distance is None or distance < best_distance:
                best_distance = distance
                ticker = symbol
    return ticker


def parse_signal(text):
    taking_match = TAKING_RE.search(text)
    if not taking_match:
        return None

    strike_match = None
    for m in STRIKE_RE.finditer(text):
        if m.start() >= taking_match.start():
            strike_match = m
            break
    if not strike_match:
        return None

    direction = "CALL" if strike_match.group(2)[0].lower() == "c" else "PUT"

    ticker = find_nearest_ticker(text, strike_match.start())
    if not ticker:
        return None

    return {
        "ticker": ticker,
        "direction": direction,
        "strike": strike_match.group(1),
    }


def main():
    text = input("Enter a message: ").strip()
    signal = parse_signal(text)
    if not signal:
        print("Not a recognized 'taking' signal.")
        return
    print(f"Ticker: {signal['ticker']}")
    print(f"Direction: {signal['direction']}")
    print(f"Strike: {signal['strike']}")


if __name__ == "__main__":
    main()
