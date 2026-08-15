"""
Regex-only classifier: resolves a Discord message from Casey to ENTRY or
EXIT when the phrasing is unambiguous, or returns None for everything else
(TRIM, ADD, and plain commentary all look the same to a regex at a glance —
see llm_classifier.py, which is what actually decides between those three
for any message this module can't confidently place).

ENTRY/EXIT stay regex-only deliberately: near-zero latency and fully
auditable, and the patterns below were mined from Team2Trading.txt rather
than guessed, since the same words show up both as trade actions and as
plain chart commentary (e.g. "closed" almost always describes a candle
close, not a position exit). Known limitation: a message that both exits
one ticker and enters another in the same line (e.g. "I'm out of spy here,
taking qqq 400c") only yields the first (highest-priority) signal.

One piece of short-lived state: Casey routinely calls out a contract in one
message ("watching qqq 298c", "Looking at the 384p on qqq" — no entry verb,
so these fall through to None/LLM/NOISE same as always) and then confirms
the entry several messages later with a bare "I'm in" / "In @ .46" that
names no ticker at all (confirmed against Team2Trading.txt: "watching the
394c" -> ... -> "Everyone im in @ .62" three messages later). Without
memory of the earlier message, the confirmation alone is indistinguishable
from noise and neither this module nor llm_classifier.py (which can't even
emit ENTRY) could ever catch it. _pending below tracks only the single
most-recently-watched contract, cleared on use/timeout/supersession —
matches this project's "in-memory state is fine pre-live-trading" stance.

A second, separate piece of state: Casey also often establishes a ticker in
one message ("that first SPY setup popped quick...") and then calls out a
strike several messages later without repeating it ("looking at the 780c on
this pullback") — a message that itself has nothing to do with a
watch/confirm pair, just plain commentary that happens to name a ticker.
_last_ticker tracks the single most-recently-mentioned ticker from ANY
message (not just watch/entry-shaped ones) and is used as a fallback only
when a message's own text names no ticker at all. Validated against
Team2Trading.txt before building this: using the single most-recent ticker
within LAST_TICKER_WINDOW_SECS matched the ticker Casey actually confirmed
later for the same strike in 98.1% of resolvable cases (100% on the
higher-stakes ENTRY-shaped ones); see LAST_TICKER_WINDOW_SECS for why the
window is bounded rather than session-long.
"""

import re
import time
from dataclasses import dataclass, field
from enum import Enum

from signal_parser import STRIKE_RE, TICKER_ALIASES, find_nearest_ticker

MENTION_RE = re.compile(r"@everyone|@here", re.IGNORECASE)
CURLY_APOSTROPHE_RE = re.compile(r"[‘’‛]")

# a period/!/? followed by whitespace-or-end, but not one sitting inside a
# decimal price like "1.05" or ".73" (those are never preceded by a digit
# *and* followed by one) — used to scope qualifier words to the same
# sentence as the closing verb they're meant to describe.
SENTENCE_BOUNDARY_RE = re.compile(r"(?<!\d)[.!?]+(?=\s|$)")

# a qualifier anywhere in the closing verb's sentence means the message is
# NOT a clean full close (it's a partial trim, or otherwise not confidently
# EXIT) — the exact size/fraction no longer matters here since TRIM
# quantity is now purely config-driven (risk.trim_pct), not parsed from
# the message. See llm_classifier.py for where these land.
QUALIFIER_RE = re.compile(
    r"\b3/4\b|\bhalf\b|\bheavy\b|\bmost\b|\ba\s+little\b|\ba\s+bit\b|\bsome\b",
    re.IGNORECASE,
)

FULL_WORD_RE = re.compile(
    r"\bfull(?:y)?\b|\ball\b|\bcompletely\b|\bthe\s+rest\b|\bremaining\b",
    re.IGNORECASE,
)

# "once we tap 30%... we can get some trims" / "I'll start scaling out
# when we break high of day" — a condition that hasn't happened yet, not a
# live action. Deliberately narrow: broader tense words like "already" or
# "I'll" alone are this trader's normal way of announcing a real live trim
# ("I'll be trimming some before 10am", "Already up 50% trim some into the
# strength"), so excluding on those would silently drop real signals —
# checked against trim_conditions.txt, this exact "when/once/if we ..."
# clause shape never appears in a real trim/exit, only in the 2 hypothetical
# cases above.
CONDITIONAL_SETUP_RE = re.compile(r"\bwhen\s+we\b|\bonce\s+we\b|\bif\s+we\b", re.IGNORECASE)

# (regex, verb_key) checked in order; first match determines the closing verb.
CLOSING_TRIGGERS = [
    (re.compile(r"\bstopp(?:ed|ing)\b", re.IGNORECASE), "stopped_out"),
    (re.compile(r"\btook\s+the\s+l\b", re.IGNORECASE), "took_the_l"),
    (re.compile(r"\bi'?m\s+(?:fully\s+|completely\s+)?out\b(?!\s+of\s+time)", re.IGNORECASE), "out"),
    (re.compile(r"\bi\s+am\s+(?:fully\s+|completely\s+)?out\b(?!\s+of\s+time)", re.IGNORECASE), "out"),
    (re.compile(r"^out\b(?!\s+of\s+time)", re.IGNORECASE), "out"),
    (re.compile(r"\bsold\b(?!\s+off\b)", re.IGNORECASE), "sold"),
    (re.compile(r"\b(?:took|taking)\b(?:\s+\S+){0,4}?\s+off\b", re.IGNORECASE), "took_off"),
    (re.compile(r"\bclosed\s+(?:these|this|my|the)?\s*(?:runners?|position|contracts?|cons)\b.*\bout\b", re.IGNORECASE), "closed_out"),
    (re.compile(r"\bscal(?:e|ing)\s+out\b", re.IGNORECASE), "scale_out"),
    (re.compile(r"\btrim(?:ming|med|s)?\b", re.IGNORECASE), "trim"),
    (re.compile(r"\bi'?m\s+selling\b|\bi\s+am\s+selling\b", re.IGNORECASE), "selling"),
    (re.compile(r"(?:^|[.!?]\s+)selling\b", re.IGNORECASE), "selling"),
]

# verb_key's whose bare (no-qualifier) form is a confident full close.
# Anything else found by CLOSING_TRIGGERS ("trim", "selling", "scale_out")
# is only ever EXIT here when FULL_WORD_RE also matches — on its own it's
# ambiguous and now routes to the LLM instead of defaulting to TRIM.
EXIT_DEFAULT_VERBS = {"stopped_out", "took_the_l", "out", "sold", "closed_out"}

ENTRY_VERB_RE = re.compile(
    r"\bi'?m\s+taking\b|\btaking\b|\bi\s+got\b|\bi\s+(?:just\s+)?(?:added|adding)\b",
    re.IGNORECASE,
)

# "watching qqq 298c", "Looking at the 384p on qqq" — the pre-entry callout
# that a later bare "I'm in" confirms. Deliberately narrow to these two verbs
# (the only ones seen preceding a strike in this shape across
# Team2Trading.txt) rather than any strike mention, so an unrelated aside
# about an already-open position doesn't get cached as "the thing Casey's
# about to enter."
WATCHING_RE = re.compile(r"\bwatching\b|\blooking\s+at\b", re.IGNORECASE)

# The entire message, after stripping @everyone/@here, must be just this —
# "I'm in", "In", optionally with a fill price ("In @ .46") — deliberately
# strict. A looser match (allowing trailing words) would also catch "In
# placing a 20% stop on this", "In that first trim area for me", "In the
# money and looking good", etc., all real Team2Trading.txt lines that start
# with "In" but aren't an entry confirmation. require_confirmation can be
# false in config.yaml (real IBKR orders), so a false positive here is worse
# than the false negative on the rarer "im in on the break" phrasing this
# misses.
CONFIRMATION_RE = re.compile(
    r"^(?:i'?m\s+in|in)\s*(?:@\s*(?:\d+\.?\d*|\.\d+))?$",
    re.IGNORECASE,
)

# how long a watched contract stays eligible to be confirmed by a later bare
# "I'm in" before it's considered stale chatter about something else.
PENDING_WINDOW_SECS = 10 * 60

# the single most-recently-watched contract: {"ticker", "direction",
# "strike", "timestamp"} or None. Single-slot by design — Casey talks
# through one setup at a time in this channel; there's no observed case of
# two contracts being watched concurrently before either is confirmed.
_pending = None

# how long a ticker mentioned anywhere (not just a watch/entry callout) stays
# eligible to fill in for a later entry/watch message that gives a strike but
# no ticker of its own. Bounded deliberately, not session-long/"until another
# ticker is mentioned": corpus validation (614 real entry/watch-shaped
# messages missing an in-message ticker, checked against the ticker Casey
# actually confirmed later for the same strike) showed the single
# most-recently-mentioned ticker within this window was right 305/311 times
# (98.1%) where a ground truth existed — 11/11 (100%) on the higher-stakes
# ENTRY-shaped cases specifically, which fire an order immediately with no
# separate confirmation step. The handful of misses were all lower-stakes
# WATCH-shaped callouts (which still need a bare "I'm in" confirmation before
# anything trades) and all involved two tickers (usually SPY/QQQ) being
# actively discussed within the same few minutes — exactly the case a wider
# or unbounded window would make worse, not better, since the cases with
# nothing mentioned in the last 15 minutes are the stale-context ones most
# likely to guess wrong if allowed to reach back further.
LAST_TICKER_WINDOW_SECS = 15 * 60

# the single most-recently-mentioned ticker, from ANY message: {"ticker",
# "timestamp"} or None. Same "most recent wins" convention as _pending, but
# expires purely by time (LAST_TICKER_WINDOW_SECS) rather than "until
# superseded" — a stale mention from outside the window must never be used
# just because nothing newer happened to come along since.
_last_ticker = None


def reset_pending_state():
    """Test-only: clear _pending/_last_ticker so fixtures don't leak state
    across runs."""
    global _pending, _last_ticker
    _pending = None
    _last_ticker = None


def _track_ticker_mention(text, now):
    """Update _last_ticker to the rightmost ticker alias mentioned in text,
    if any. Called on every message regardless of what else it resolves to
    — plain commentary ("I like qqq here above our zone also") is exactly
    the kind of message that establishes context for a later ticker-less
    callout, so this can't be scoped to only watch/entry-shaped messages."""
    global _last_ticker
    best_pos = None
    best_symbol = None
    for alias, symbol in TICKER_ALIASES.items():
        for m in re.finditer(r"\b" + re.escape(alias) + r"\b", text, re.IGNORECASE):
            if best_pos is None or m.start() > best_pos:
                best_pos = m.start()
                best_symbol = symbol
    if best_symbol:
        _last_ticker = {"ticker": best_symbol, "timestamp": now}


def _resolve_ticker(text, anchor_pos, now):
    """find_nearest_ticker on this message; if this message names no ticker
    at all, fall back to _last_ticker within LAST_TICKER_WINDOW_SECS.
    Returns (ticker_or_None, inferred_from_recent_context_bool)."""
    ticker = find_nearest_ticker(text, anchor_pos)
    if ticker:
        return ticker, False
    if _last_ticker is not None and now - _last_ticker["timestamp"] <= LAST_TICKER_WINDOW_SECS:
        return _last_ticker["ticker"], True
    return None, False


class SignalType(Enum):
    ENTRY = "ENTRY"
    TRIM = "TRIM"
    ADD = "ADD"
    EXIT = "EXIT"
    NOISE = "NOISE"


@dataclass
class Signal:
    type: SignalType
    ticker: str = None
    direction: str = None  # CALL / PUT, ENTRY only
    reason: str = ""
    raw_text: str = ""
    db_id: int = None  # row id in db.signals, set by alerting.log_signal; lets
                        # trade_executor write its outcome back onto that row
    received_at: float = field(default_factory=time.time)  # wall-clock time this
                        # signal was classified — trade_executor's reconnect.*
                        # timeout is measured from here, not from IBKR disconnect


def _strip_mentions(text):
    text = CURLY_APOSTROPHE_RE.sub("'", text)
    return MENTION_RE.sub("", text).strip()


def _sentence_span(text, pos):
    """(start, end) of the sentence around character index pos. Confines
    qualifier-word matching to the same sentence as the closing verb, so a
    word like "little" describing something unrelated in a later sentence
    ("I stopped out here in this candle. Raised my stop a little tight
    there") doesn't get misread as a sizing qualifier for that verb — that
    specific case was misclassifying a full stop-out as a partial trim."""
    sentence_start = 0
    sentence_end = len(text)
    for m in SENTENCE_BOUNDARY_RE.finditer(text):
        if m.end() <= pos:
            sentence_start = m.end()
        elif m.start() >= pos:
            sentence_end = m.start()
            break
    return sentence_start, sentence_end


def _classify_closing_action(text):
    """Returns (SignalType.EXIT, verb_key) for a confident full close, or
    (None, None) for anything else — including a partial trim, since that
    no longer gets decided here (see module docstring)."""
    for pattern, verb_key in CLOSING_TRIGGERS:
        match = pattern.search(text)
        if not match:
            continue

        if verb_key == "took_off" and STRIKE_RE.search(text, match.start()):
            # "taking QQQ 289p off that level" describes a new entry (an
            # option strike right after the verb), not a trim — real trims
            # ("took some off the table", "taking most off here") never
            # mention a strike at/after took/taking, only ever earlier in
            # the message as context for the position already open (e.g.
            # "558c now. Taking most off ...").
            continue

        sentence_start, sentence_end = _sentence_span(text, match.start())
        sentence = text[sentence_start:sentence_end]

        if CONDITIONAL_SETUP_RE.search(sentence):
            continue

        if FULL_WORD_RE.search(sentence):
            return SignalType.EXIT, verb_key
        if verb_key in EXIT_DEFAULT_VERBS and not QUALIFIER_RE.search(sentence):
            return SignalType.EXIT, verb_key
        return None, None  # partial, or an ambiguous non-default verb — LLM's call

    return None, None


def classify(raw_text, now=None):
    """Returns a Signal(ENTRY) or Signal(EXIT) when the regex is confident,
    else None — callers must route None to llm_classifier.classify() to
    decide TRIM / ADD / NOISE.

    now: injectable for tests exercising the watched-contract timeout;
    production callers can omit it (defaults to time.time())."""
    global _pending
    if now is None:
        now = time.time()
    text = _strip_mentions(raw_text)
    _track_ticker_mention(text, now)

    action_type, verb_key = _classify_closing_action(text)
    if action_type is not None:
        # deliberately NOT using _resolve_ticker's fallback here: EXIT/TRIM/
        # ADD already get checked against IBKR's actual open positions before
        # anything trades, so a ticker-less EXIT is already handled safely by
        # that gate. ENTRY has no such backstop (there's no existing position
        # to reconcile a wrong guess against), which is what the fallback
        # below was validated for.
        ticker = None
        for pattern, key in CLOSING_TRIGGERS:
            m = pattern.search(text)
            if m and key == verb_key:
                ticker = find_nearest_ticker(text, m.start())
                break
        return Signal(
            type=action_type,
            ticker=ticker,
            reason=f"closing trigger '{verb_key}'",
            raw_text=raw_text,
        )

    entry_match = ENTRY_VERB_RE.search(text)
    if entry_match:
        strike_match = None
        for m in STRIKE_RE.finditer(text):
            if m.start() >= entry_match.start():
                strike_match = m
                break
        if strike_match:
            ticker, inferred = _resolve_ticker(text, strike_match.start(), now)
            if ticker:
                direction = "CALL" if strike_match.group(2)[0].lower() == "c" else "PUT"
                # this message is itself a confident, self-contained entry —
                # any earlier watched contract is now moot (either it's this
                # same one, or Casey moved on without confirming it).
                _pending = None
                reason = (
                    f"entry verb + strike, ticker inferred from recent context ({ticker})"
                    if inferred else "entry verb + ticker + strike"
                )
                return Signal(
                    type=SignalType.ENTRY,
                    ticker=ticker,
                    direction=direction,
                    reason=reason,
                    raw_text=raw_text,
                )
        # entry-shaped but no ticker/strike resolved — not confident enough
        # to call it ENTRY; fall through like everything else below

    if (
        CONFIRMATION_RE.match(text)
        and _pending is not None
        and now - _pending["timestamp"] <= PENDING_WINDOW_SECS
    ):
        signal = Signal(
            type=SignalType.ENTRY,
            ticker=_pending["ticker"],
            direction=_pending["direction"],
            reason=f"confirmation of watched {_pending['ticker']} {_pending['strike']}{'c' if _pending['direction'] == 'CALL' else 'p'}",
            raw_text=raw_text,
        )
        _pending = None
        return signal

    watching_match = WATCHING_RE.search(text)
    if watching_match:
        strike_match = None
        for m in STRIKE_RE.finditer(text):
            if m.start() >= watching_match.start():
                strike_match = m
                break
        if strike_match:
            ticker, _inferred = _resolve_ticker(text, strike_match.start(), now)
            if ticker:
                direction = "CALL" if strike_match.group(2)[0].lower() == "c" else "PUT"
                _pending = {
                    "ticker": ticker,
                    "direction": direction,
                    "strike": strike_match.group(1),
                    "timestamp": now,
                }

    return None
