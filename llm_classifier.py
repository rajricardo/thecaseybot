"""
Live routing for any Discord message signal_classifier.classify() couldn't
confidently resolve to ENTRY or EXIT (it returns None in that case — see
its module docstring). Everything else — a full close phrased in a way the
regex doesn't recognize, a genuine partial trim, an add to an existing
position, or plain commentary — gets a single Claude call here to decide
EXIT / TRIM / ADD / NOISE. EXIT is included here (not just in the regex
path) because real trader phrasing for a full close is often future-tense
or split across sentences ("The rest of my puts are getting destroyed. I'm
gonna sell them.") in ways the regex's same-sentence, present-tense patterns
don't catch — see Team2Trading.txt for how common this shape is.

Uses AsyncAnthropic, not the sync client llm_trim_evaluator.py uses: this
runs inline in discord_listener's asyncio event loop, and a blocking call
here would stall the Discord connection (heartbeats, other events) for
however long a live call takes — see Notes.md-adjacent latency testing,
roughly 1-2s per uncached call. A timeout is set so one slow/stuck request
can't hang the listener indefinitely.

Fails safe: any error, timeout, or malformed response falls back to NOISE
rather than risking an unintended order from a degraded classification —
symmetric with signal_classifier's own "not confident -> don't guess"
stance on ENTRY.
"""

import logging

import anthropic

from signal_classifier import Signal, SignalType

_logger = logging.getLogger("casey_bot")

REQUEST_TIMEOUT_SECS = 10.0

SYSTEM_PROMPT = """You are routing a live Discord message from a day-trading options-alert channel (0DTE SPY/QQQ/IWM-style calls/puts). A fast regex classifier already ruled out this message as a clean ENTRY (opening a brand new position) or a clean, unconditional EXIT/TRIM/ADD phrasing it recognizes — decide which of these four it actually is:

EXIT  - fully closing the ENTIRE remaining position right now: "selling all/everything/the rest/what's left", "I'm out", "closed it all", "sold mine" — the whole thing, not a fraction.
TRIM  - reducing part of an already-open position right now (selling half/some/most/a few, "taking some off", "scaling out", "trimming", "locking in some gains") — anything short of closing the whole thing.
ADD   - adding more to an existing position at the same ticker right now (averaging in), e.g. "adding more here", "adding this dip", "adding a few puts here".
NOISE - anything else: chart/market commentary, advice or encouragement to the channel that isn't the trader's own live action, a conditional/future intent that hasn't happened yet, a past-tense recap, or any other non-actionable text.

The single most common error here is conditional/future intent — a plan for what the trader WILL do if some price level hits, not something happening now. This is NOISE, never EXIT/TRIM/ADD, no matter how specific or confident the wording sounds:
  - Any "if/when/once/unless" clause naming the trigger, in either order: "I'll sell the rest if we break the 13ema", "If this doesn't hold I'm gonna sell the rest of my puts", "once we tap 30% we can get some trims", "needs to hold here or I'll sell the rest".
  - A watched level framed as the reason to act later, even without if/when: "waiting for one more push to sell the rest", "looking at this flag break to add some more", "I'm looking at adding back those runners I sold".
  - "Thinking of" / "planning to" / "want to see X" / "looking to" hedging: "thinking of adding those back", "want to see the 13ema hold and I will add some more".
Contrast with the real, present-tense action that should get EXIT/TRIM/ADD: no if/when/once clause, no "looking to"/"thinking of" hedging — the trader states what they're doing right now, e.g. "I'm gonna sell the rest of spy here", "adding here @ .42", "selling half here".

If a ticker is named or clearly implied (SPY/QQQ/IWM/TSLA/AMD/AAPL/NVDA/MSFT/AMZN/META/GOOGL/NFLX/GLD/MAGS/SLV, or a company name), put its uppercase symbol in `ticker`; otherwise leave it an empty string. Judge only the message given. Be decisive — pick the single best label even for terse, informal phrasing ("Locking in more", "Out half", "Adding here", "Selling the rest" are all real trade actions, not noise, even without a ticker attached)."""

ROUTE_TOOL = {
    "name": "route_signal",
    "description": "Classify this message as EXIT, TRIM, ADD, or NOISE.",
    "input_schema": {
        "type": "object",
        "properties": {
            "label": {"type": "string", "enum": ["EXIT", "TRIM", "ADD", "NOISE"]},
            "ticker": {"type": "string", "description": "Uppercase ticker symbol mentioned/implied, or empty string if none"},
        },
        "required": ["label", "ticker"],
    },
}


async def classify(text, client, model):
    """text: the raw (mention-unstripped is fine) Discord message.
    client: an anthropic.AsyncAnthropic instance, created once at startup.
    Returns a Signal — never raises."""
    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=256,
            system=SYSTEM_PROMPT,
            tools=[ROUTE_TOOL],
            tool_choice={"type": "tool", "name": "route_signal"},
            messages=[{"role": "user", "content": text}],
            timeout=REQUEST_TIMEOUT_SECS,
        )
    except Exception as e:
        _logger.exception(f"[llm_classifier] API call failed for {text!r} — treating as NOISE")
        return Signal(type=SignalType.NOISE, reason=f"llm_classifier error: {e}", raw_text=text)

    for block in resp.content:
        if block.type == "tool_use":
            label = block.input.get("label")
            if label not in ("EXIT", "TRIM", "ADD", "NOISE"):
                _logger.warning(f"[llm_classifier] unexpected label {label!r} for {text!r} — treating as NOISE")
                return Signal(type=SignalType.NOISE, reason=f"llm_classifier returned unrecognized label {label!r}", raw_text=text)
            # uppercase/stripped defensively — _resolve_target_position
            # matches this against contract.symbol with a plain ==, and the
            # regex path's ticker resolution (signal_parser.TICKER_ALIASES)
            # is always uppercase already, so a lowercase/mixed-case ticker
            # here would silently fail to match a real open position and
            # get skipped as if nothing were held.
            ticker = (block.input.get("ticker") or "").strip().upper() or None
            return Signal(
                type=SignalType[label],
                ticker=ticker,
                reason="llm_classifier",
                raw_text=text,
            )

    _logger.warning(f"[llm_classifier] no tool_use block in response for {text!r} — treating as NOISE")
    return Signal(type=SignalType.NOISE, reason="llm_classifier: no tool_use block in response", raw_text=text)
