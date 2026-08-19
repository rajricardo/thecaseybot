"""
Wires classified signals into actual IBKR orders.

ENTRY, and most EXIT, come from signal_classifier.py's regex path (fast,
deterministic). TRIM, ADD, and any EXIT phrased in a way the regex doesn't
recognize (future tense, or split across sentences) come from
llm_classifier.py — Claude's call on any message the regex couldn't
confidently place — since "half"/"some"/"trimming"/"adding"/"the rest"
language is genuinely ambiguous between a partial trim, a full close, an
add, and plain commentary in a way regex kept getting wrong at scale (see
signal_classifier.py's docstring).

ADD buys risk.add_pct% of whatever is CURRENTLY held (same
applied-fresh-each-time convention as TRIM, no cap on how large repeated
ADDs can grow a position — deliberately, per how it was scoped). If a
protective stop from an earlier TRIM is active on the contract, it's
cancelled *before* the buy (same race-avoidance reasoning as TRIM/EXIT
below) and replaced afterward with a new one sized to the post-add total
quantity at the (possibly now-blended) avg entry price — read back live
from IBKR rather than computed locally, since this bot never waits for a
fill and the true blended cost only exists once the buy actually fills.
If there was no stop to begin with, ADD doesn't create one from scratch —
that stays TRIM's job.

ENTRY: risk-gate the ticker (allowed_tickers, and refuses to open a second,
possibly differently-struck contract on a ticker that already has one open —
use ADD for that instead), derive the contract from config's
strike_offset/expiry_selection (ibkr_client.select_contract_params +
find_option_contract), then place it at price_type.

Contract count comes from risk.sizing_mode: "fixed" (default) uses
max_contracts_per_trade as-is, same as before this was configurable.
"dynamic" instead derives qty from risk.capital_per_trade and the
contract's LAST price — floor(capital_per_trade / (LAST * multiplier)) — so
a fixed dollar amount buys a smaller quantity of expensive contracts and a
larger quantity of cheap ones. The LAST price is fetched once via
ibkr_client.get_entry_price_snapshot right after the contract is resolved,
and that same Ticker snapshot is threaded through to place_order(tkr=...)
so pricing the order itself (when price_type isn't MARKET) doesn't poll
IBKR a second time for data already in hand. An entry that can't afford
even 1 contract at that price (or gets no usable LAST price at all) is
skipped and logged, same as every other entry risk-gate failure below —
never rounded up to 1, since that could silently place an order sized
beyond what capital_per_trade allows.

TRIM/EXIT: resolve the ticker to an actually-held IBKR position (never our
own bookkeeping — a live ib.positions() query) before placing any SELL.
A TRIM/EXIT for a ticker with nothing open, or with no ticker named in the
message and more than one thing open, is skipped and logged rather than
guessed at — see _resolve_target_position. This exists specifically to
prevent e.g. a QQQ "I'm out" message firing a sell while only SPY is
actually held.

TRIM sells risk.trim_pct% of whatever is CURRENTLY held at the moment the
signal fires — applied fresh each time, not against the original entry
size, so a second trim on the same position trims risk.trim_pct% of
whatever's left after the first, and so on for however many trims arrive.
Any stale protective stop from an earlier trim (sized for a since-changed
quantity) is cancelled *before* the new sell goes out, not after — placing
a smaller sell while a stop for the old, larger quantity is still live
risks both orders racing to sell the same contracts. Once the sell is in,
a fresh stop sized to exactly what's left is placed at the position's
average entry price (avg_premium from ibkr_client.get_open_option_position)
if risk.auto_submit_stop_loss is true and there's anything left to protect
— so runners can give back unrealized gains but can't turn into a capital
loss. EXIT cancels any pending stop the same way (before the final sell)
since nothing will be left for it to protect.

max_concurrent_positions is gated in handle_entry off a live
get_open_option_positions count. max_daily_losing_trades / daily_loss_limit
are gated in handle_entry/handle_add off ibkr_client.daily_risk_stats() —
realized P&L tallied in-memory via commissionReportEvent/positionEvent (see
ibkr_client.py), not a persistent ledger, so the count resets on a bot
restart. Both only ever block ENTRY/ADD, never TRIM/EXIT — reducing risk
must always still be possible.

The risk config (allowed_tickers, strike_offset, price_type, trim_pct,
auto_submit_stop_loss, require_confirmation, etc.) is re-read from
config.yaml fresh before every signal — see load_risk_config / run_worker —
so an edit made while the bot is running takes effect on the next signal,
no restart needed. ibkr connection settings (host/port/client_id) are still
only read once at startup, since changing those live would require
reconnecting.

price_type: "AUTO" is resolved (see _resolve_price_type) at the same spot
every handle_* reads risk.price_type, into MIDPOINT before 9:45am ET or
MARKET from 9:45am ET onward — IBKR doesn't accept MARKET orders on options
before then. ibkr_client.place_order itself never sees the literal string
"AUTO"; only ever a concrete order type.

A signal that arrives while IBKR is disconnected (dropped wifi, a TWS/
Gateway restart, etc.) is never silently lost: ensure_connected retries the
connection indefinitely, and run_worker holds the signal on signal_queue
until it's back rather than failing it outright. What happens once
reconnected is governed by config.yaml's reconnect.* (see
load_reconnect_config / _reconnect_skip_reason): retry_on_reconnect=false
discards it instead of placing it; retry_timeout_mins discards it if the
wait — measured from when the signal was originally classified, not from
when IBKR went down — ran longer than that. Both default to "always place
it, no matter how long the outage" when reconnect.* is omitted from
config.yaml, matching this bot's false-negatives-are-worse-than-false-
positives risk posture.
"""

import asyncio
import queue
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import yaml

import db
import ibkr_client
from signal_classifier import SignalType

# IBKR doesn't accept MARKET orders on options before 9:45am ET (prices are
# still settling right after the open) — risk.price_type: "AUTO" exists so
# a signal firing right at the open doesn't need a human to remember to
# flip price_type by hand. See _resolve_price_type.
_MARKET_OPEN_CUTOFF_ET = dt_time(9, 45)
_EASTERN = ZoneInfo("America/New_York")


def _resolve_price_type(price_type, now=None):
    """AUTO isn't a real IBKR order type — it's resolved here, once, right
    where each handle_* reads risk.price_type, so every downstream use
    (the CONFIRMATION REQUIRED message, db.insert_order, place_order
    itself) sees the concrete MIDPOINT/MARKET it actually resolved to
    rather than the literal string "AUTO". Resolved fresh against the
    current wall-clock time on every call (not cached) — same
    read-fresh-every-signal posture as the rest of risk_cfg — using
    US/Eastern local time (DST-aware) since that's what the market's own
    9:30am open is defined in, not a fixed UTC offset.
    now: an aware datetime to resolve against, for tests; None (the
    default) uses the real current moment, same injection pattern as
    _reconnect_skip_reason's reconnected_at."""
    if price_type.upper() != "AUTO":
        return price_type
    now_et = (now or datetime.now(_EASTERN)).astimezone(_EASTERN).time()
    return "MIDPOINT" if now_et < _MARKET_OPEN_CUTOFF_ET else "MARKET"


def _daily_halt_reason(risk_cfg):
    """None if new risk-increasing orders (ENTRY/ADD) are still allowed
    today, else a string explaining why they're not. Deliberately never
    consulted by TRIM/EXIT — those reduce risk and must never be blocked
    by a daily limit; the whole point is to still be able to get out."""
    daily_pnl, losing_trades = ibkr_client.daily_risk_stats()

    max_losers = risk_cfg.get("max_daily_losing_trades")
    if max_losers is not None and losing_trades >= max_losers:
        return (f"max_daily_losing_trades ({max_losers}) reached today "
                f"({losing_trades} losing trades)")

    loss_limit = risk_cfg.get("daily_loss_limit")
    if loss_limit is not None and daily_pnl <= -abs(loss_limit):
        return (f"daily_loss_limit (${loss_limit}) reached (today's realized "
                f"P&L: ${daily_pnl:.2f})")

    return None


def handle_entry(ib, signal, risk_cfg, logger):
    ticker = signal.ticker
    allowed = risk_cfg.get("allowed_tickers") or []
    if allowed and ticker not in allowed:
        msg = f"SKIP entry {ticker} {signal.direction}: not in allowed_tickers {allowed}"
        logger.info(msg)
        db.update_signal_outcome(signal.db_id, msg, ticker, failed=True)
        return None

    halt_reason = _daily_halt_reason(risk_cfg)
    if halt_reason:
        msg = (f"SKIP entry {ticker} {signal.direction}: {halt_reason} — no new entries "
               f"for the rest of today (raw: {signal.raw_text!r})")
        logger.info(msg)
        db.update_signal_outcome(signal.db_id, msg, ticker, failed=True)
        return None

    open_positions = ibkr_client.get_open_option_positions(ib)

    max_concurrent = risk_cfg.get("max_concurrent_positions")
    if max_concurrent is not None and len(open_positions) >= max_concurrent:
        msg = (f"SKIP entry {ticker} {signal.direction}: max_concurrent_positions "
               f"({max_concurrent}) already reached ({len(open_positions)} open)")
        logger.info(msg)
        db.update_signal_outcome(signal.db_id, msg, ticker, failed=True)
        return None

    # A fresh ENTRY while this ticker already has a position open would risk
    # landing on a different strike (strike_offset is computed off the live
    # underlying price each time) and leaving two distinct contracts open
    # under one ticker — exactly the ambiguity _resolve_target_position has
    # to refuse to guess through for every later TRIM/EXIT/ADD. Blocked
    # here instead, so it can't arise in the first place; an ADD signal is
    # the way to add to an existing position.
    existing = [c for c, _q, _a in open_positions if c.symbol == ticker]
    if existing:
        held = ", ".join(sorted(f"{c.lastTradeDateOrContractMonth} {c.strike}{c.right}"
                                 for c in existing))
        msg = (f"SKIP entry {ticker} {signal.direction}: already have an open position "
               f"for this ticker ({held}) — close it out before a fresh entry, or this "
               f"should be an ADD (raw: {signal.raw_text!r})")
        logger.info(msg)
        db.update_signal_outcome(signal.db_id, msg, ticker, failed=True)
        return None

    strike_selection = risk_cfg.get("strike_offset", "1OTM")
    expiry_selection = risk_cfg.get("expiry_selection", "nearest")
    expiry, strikes = ibkr_client.select_contract_params(
        ib, ticker, signal.direction, strike_selection, expiry_selection
    )
    if not expiry or not strikes:
        msg = (f"SKIP entry {ticker} {signal.direction}: could not derive a contract "
               f"(expiry={expiry}, strikes={strikes})")
        logger.info(msg)
        db.update_signal_outcome(signal.db_id, msg, ticker, failed=True)
        return None

    try:
        contract = ibkr_client.find_option_contract(ib, ticker, signal.direction, expiry, strikes)
    except ValueError as e:
        msg = f"SKIP entry {ticker} {signal.direction}: {e}"
        logger.info(msg)
        db.update_signal_outcome(signal.db_id, msg, ticker, failed=True)
        return None

    price_type = _resolve_price_type(risk_cfg.get("price_type", "MIDPOINT"))
    contract_label = f"{ticker} {expiry} {contract.strike}{contract.right}"

    tkr = None
    if risk_cfg.get("sizing_mode", "fixed") == "dynamic":
        capital_per_trade = risk_cfg.get("capital_per_trade") or 0
        last_price, tkr = ibkr_client.get_entry_price_snapshot(ib, contract)
        if not last_price or last_price <= 0:
            msg = (f"SKIP entry {ticker} {signal.direction}: sizing_mode is dynamic but no "
                   f"LAST price is available for {contract_label} — can't size the order "
                   f"(raw: {signal.raw_text!r})")
            logger.info(msg)
            db.update_signal_outcome(signal.db_id, msg, ticker, failed=True)
            return None
        multiplier = float(contract.multiplier or 100)
        cost_per_contract = last_price * multiplier
        qty = int(capital_per_trade // cost_per_contract)
        if qty < 1:
            msg = (f"SKIP entry {ticker} {signal.direction}: capital_per_trade "
                   f"(${capital_per_trade:.2f}) can't afford even 1 contract of "
                   f"{contract_label} at LAST=${last_price:.2f} (${cost_per_contract:.2f}/contract) "
                   f"(raw: {signal.raw_text!r})")
            logger.info(msg)
            db.update_signal_outcome(signal.db_id, msg, ticker, failed=True)
            return None
    else:
        qty = risk_cfg.get("max_contracts_per_trade", 1)

    if risk_cfg.get("require_confirmation", True):
        msg = (
            f"CONFIRMATION REQUIRED, order NOT submitted: BUY x{qty} {contract_label} "
            f"[{price_type}] (raw: {signal.raw_text!r}). "
            f"Set risk.require_confirmation: false to auto-submit."
        )
        logger.info(msg)
        db.insert_order(signal.db_id, "signal", "BUY", ticker, contract_label, qty, price_type,
                         status="blocked", detail=msg)
        db.update_signal_outcome(signal.db_id, msg, contract_label, failed=True)
        return None

    logger.info(f"[trade_executor] Ready to fire, Dracarys... BUY x{qty} {contract_label}")
    trade = ibkr_client.place_order(ib, contract, "BUY", qty, price_type, tkr=tkr)
    outcome = f"ENTRY placed: BUY x{qty} {contract_label} ibkr_order_id={trade.order.orderId}"
    logger.info(outcome)
    db.insert_order(signal.db_id, "signal", "BUY", ticker, contract_label, qty, price_type,
                     ibkr_order_id=trade.order.orderId, status="submitted", detail=outcome)
    db.update_signal_outcome(signal.db_id, outcome, contract_label, failed=False)
    return trade


def _resolve_target_position(ib, signal, logger, action_label):
    """Figures out which held contract a TRIM/EXIT message refers to: the
    named ticker if the message mentioned one, else whichever single ticker
    currently has an open position (many of Casey's exit messages don't
    repeat the ticker, e.g. "I'm out"). Refuses to guess when there's more
    than one open position and no ticker was named, when the named ticker
    has nothing open, or when the named ticker itself has more than one
    distinct contract open (e.g. an ENTRY resolved to a different strike
    than a position already open for the same ticker — strike_offset is
    computed fresh off the live underlying price each time, so the exact
    same signal text can land on a different strike run to run) — the exact
    scenario this check exists to prevent: a TRIM/stop silently landing on
    the wrong strike while the real position sits unprotected. Returns
    (contract, qty, avg_premium) or (None, 0, None)."""
    open_positions = ibkr_client.get_open_option_positions(ib)

    if signal.ticker:
        matches = [(c, q, a) for c, q, a in open_positions if c.symbol == signal.ticker]
        if not matches:
            msg = (f"SKIP {action_label} {signal.ticker}: no open IBKR position for "
                   f"this ticker (raw: {signal.raw_text!r})")
            logger.info(msg)
            db.update_signal_outcome(signal.db_id, msg, signal.ticker, failed=True)
            return None, 0, None
        if len(matches) > 1:
            contracts = sorted(f"{c.lastTradeDateOrContractMonth} {c.strike}{c.right}"
                                for c, _, _ in matches)
            msg = (f"SKIP {action_label} {signal.ticker}: more than one open contract "
                   f"for this ticker ({contracts}) — refusing to guess which one "
                   f"(raw: {signal.raw_text!r})")
            logger.info(msg)
            db.update_signal_outcome(signal.db_id, msg, signal.ticker, failed=True)
            return None, 0, None
        return matches[0]

    if not open_positions:
        msg = (f"SKIP {action_label}: no ticker in message and no open IBKR positions "
               f"at all (raw: {signal.raw_text!r})")
        logger.info(msg)
        db.update_signal_outcome(signal.db_id, msg, None, failed=True)
        return None, 0, None
    tickers = {c.symbol for c, _, _ in open_positions}
    if len(tickers) > 1:
        msg = (f"SKIP {action_label}: no ticker in message and more than one open "
               f"position ({sorted(tickers)}) — refusing to guess which one "
               f"(raw: {signal.raw_text!r})")
        logger.info(msg)
        db.update_signal_outcome(signal.db_id, msg, None, failed=True)
        return None, 0, None
    return open_positions[0]


def handle_exit(ib, signal, risk_cfg, logger):
    contract, held_qty, _avg_premium = _resolve_target_position(ib, signal, logger, "exit")
    if contract is None:
        return None

    qty = abs(int(held_qty))
    price_type = _resolve_price_type(risk_cfg.get("price_type", "MIDPOINT"))
    contract_label = (f"{contract.symbol} {contract.lastTradeDateOrContractMonth} "
                       f"{contract.strike}{contract.right}")

    if risk_cfg.get("require_confirmation", True):
        existing_stops = ibkr_client.count_open_stop_orders(ib, contract)
        if existing_stops:
            logger.info(f"CONFIRMATION REQUIRED, stop NOT cancelled: {existing_stops} protective "
                        f"stop order(s) for {contract_label} would be cancelled before this exit.")
        msg = (
            f"CONFIRMATION REQUIRED, order NOT submitted: SELL x{qty} {contract_label} "
            f"[{price_type}] (raw: {signal.raw_text!r}). Set risk.require_confirmation: "
            f"false to auto-submit."
        )
        logger.info(msg)
        db.insert_order(signal.db_id, "signal", "SELL", contract.symbol, contract_label, qty,
                         price_type, status="blocked", detail=msg)
        db.update_signal_outcome(signal.db_id, msg, contract_label, failed=True)
        return None

    # cancel any stale protective stop *before* the sell — otherwise a stop
    # order and this exit's sell could both be live for the same contracts
    # at once.
    cancelled = ibkr_client.cancel_open_stop_orders(ib, contract)
    if cancelled:
        logger.info(f"Cancelled {cancelled} protective stop order(s) for {contract_label} "
                    f"before full exit")

    logger.info(f"[trade_executor] Ready to fire, Dracarys... SELL x{qty} {contract_label}")
    trade = ibkr_client.place_order(ib, contract, "SELL", qty, price_type)
    outcome = f"EXIT placed: SELL x{qty} {contract_label} ibkr_order_id={trade.order.orderId}"
    logger.info(outcome)
    db.insert_order(signal.db_id, "signal", "SELL", contract.symbol, contract_label, qty,
                     price_type, ibkr_order_id=trade.order.orderId, status="submitted",
                     detail=outcome)
    db.update_signal_outcome(signal.db_id, outcome, contract_label, failed=False)
    return trade


def handle_trim(ib, signal, risk_cfg, logger):
    contract, held_qty, avg_premium = _resolve_target_position(ib, signal, logger, "trim")
    if contract is None:
        return None

    held_qty = abs(int(held_qty))
    pct = risk_cfg.get("trim_pct", 50)
    qty = min(held_qty, max(1, round(held_qty * pct / 100)))
    remaining_qty = held_qty - qty
    price_type = _resolve_price_type(risk_cfg.get("price_type", "MIDPOINT"))
    auto_stop = risk_cfg.get("auto_submit_stop_loss", True)
    contract_label = (f"{contract.symbol} {contract.lastTradeDateOrContractMonth} "
                       f"{contract.strike}{contract.right}")

    if risk_cfg.get("require_confirmation", True):
        existing_stops = ibkr_client.count_open_stop_orders(ib, contract)
        if existing_stops:
            logger.info(f"CONFIRMATION REQUIRED, stop NOT cancelled: {existing_stops} existing "
                        f"protective stop order(s) for {contract_label} would be cancelled before "
                        f"this trim.")
        msg = (
            f"CONFIRMATION REQUIRED, order NOT submitted: SELL x{qty} (of {held_qty} held, "
            f"{pct}%) {contract_label} [{price_type}] (raw: {signal.raw_text!r}). "
            f"Set risk.require_confirmation: false to auto-submit."
        )
        logger.info(msg)
        if remaining_qty > 0 and auto_stop:
            if avg_premium is not None:
                logger.info(
                    f"CONFIRMATION REQUIRED, stop NOT submitted: STP SELL x{remaining_qty} "
                    f"{contract_label} @ ~{avg_premium:.2f} (protects the runners at avg entry "
                    f"price after the above trim)."
                )
            else:
                logger.info(f"No avg entry price available yet for {contract_label} — the "
                            f"protective stop for the {remaining_qty} runner(s) would be "
                            f"skipped even once confirmed.")
        db.insert_order(signal.db_id, "signal", "SELL", contract.symbol, contract_label, qty,
                         price_type, status="blocked", detail=msg)
        db.update_signal_outcome(signal.db_id, msg, contract_label, failed=True)
        return None

    # cancel any stale protective stop (sized for the pre-trim quantity)
    # *before* the sell — otherwise the old stop and this trim's sell could
    # both be live for the same contracts at once.
    cancelled = ibkr_client.cancel_open_stop_orders(ib, contract)
    if cancelled:
        logger.info(f"Cancelled {cancelled} existing protective stop order(s) for "
                    f"{contract_label} before this trim")

    logger.info(f"[trade_executor] Ready to fire, Dracarys... SELL x{qty} {contract_label}")
    trade = ibkr_client.place_order(ib, contract, "SELL", qty, price_type)
    outcome = (f"TRIM placed: SELL x{qty} (of {held_qty} held, {pct}%) {contract_label} "
               f"ibkr_order_id={trade.order.orderId}")
    logger.info(outcome)
    db.insert_order(signal.db_id, "signal", "SELL", contract.symbol, contract_label, qty,
                     price_type, ibkr_order_id=trade.order.orderId, status="submitted",
                     detail=outcome)

    if remaining_qty <= 0:
        logger.info(f"No runners left on {contract_label} after this trim — skipping "
                     f"protective stop")
        db.update_signal_outcome(signal.db_id, outcome, contract_label, failed=False)
        return trade

    if not auto_stop:
        logger.info(f"auto_submit_stop_loss is false — skipping protective stop for the "
                     f"{remaining_qty} runner(s) on {contract_label}")
        db.update_signal_outcome(signal.db_id, outcome, contract_label, failed=False)
        return trade

    if avg_premium is None:
        logger.info(f"No avg entry price available for {contract_label} — skipping "
                     f"protective stop for the {remaining_qty} runner(s)")
        db.update_signal_outcome(signal.db_id, outcome, contract_label, failed=False)
        return trade

    stop_trade = ibkr_client.place_stop_order(ib, contract, "SELL", remaining_qty, avg_premium)
    stop_outcome = (f"Protective stop placed: STP SELL x{remaining_qty} {contract_label} "
                     f"@ {stop_trade.order.auxPrice} (avg entry) "
                     f"ibkr_order_id={stop_trade.order.orderId}")
    logger.info(stop_outcome)
    db.insert_order(signal.db_id, "signal", "STP SELL", contract.symbol, contract_label,
                     remaining_qty, price_type, ibkr_order_id=stop_trade.order.orderId,
                     status="submitted", detail=stop_outcome)
    db.update_signal_outcome(signal.db_id, f"{outcome} {stop_outcome}", contract_label,
                              failed=False)
    return trade


def handle_add(ib, signal, risk_cfg, logger):
    # Checked before resolving a target position — ADD increases risk on an
    # existing position the same way ENTRY opens a new one, so it's gated by
    # the same daily halt (unlike TRIM/EXIT, which must never be blocked).
    halt_reason = _daily_halt_reason(risk_cfg)
    if halt_reason:
        msg = (f"SKIP add {signal.ticker}: {halt_reason} — no new adds for the rest of "
               f"today (raw: {signal.raw_text!r})")
        logger.info(msg)
        db.update_signal_outcome(signal.db_id, msg, signal.ticker, failed=True)
        return None

    contract, held_qty, _avg_premium = _resolve_target_position(ib, signal, logger, "add")
    if contract is None:
        return None

    held_qty = abs(int(held_qty))
    pct = risk_cfg.get("add_pct", 10)
    qty = max(1, round(held_qty * pct / 100))
    price_type = _resolve_price_type(risk_cfg.get("price_type", "MIDPOINT"))
    contract_label = (f"{contract.symbol} {contract.lastTradeDateOrContractMonth} "
                       f"{contract.strike}{contract.right}")

    if risk_cfg.get("require_confirmation", True):
        existing_stops = ibkr_client.count_open_stop_orders(ib, contract)
        if existing_stops:
            logger.info(f"CONFIRMATION REQUIRED, stop NOT cancelled: {existing_stops} existing "
                        f"protective stop order(s) for {contract_label} would be cancelled and "
                        f"replaced after this add.")
        msg = (
            f"CONFIRMATION REQUIRED, order NOT submitted: BUY x{qty} (adding {pct}% to the "
            f"{held_qty} held) {contract_label} [{price_type}] (raw: {signal.raw_text!r}). "
            f"Set risk.require_confirmation: false to auto-submit."
        )
        logger.info(msg)
        db.insert_order(signal.db_id, "signal", "BUY", contract.symbol, contract_label, qty,
                         price_type, status="blocked", detail=msg)
        db.update_signal_outcome(signal.db_id, msg, contract_label, failed=True)
        return None

    # cancel any existing stop *before* the buy, same reasoning as
    # TRIM/EXIT: its quantity/price were sized for the pre-add position and
    # are about to be stale either way, so there's no reason to leave it
    # live while the position size it was protecting changes underneath it.
    cancelled = ibkr_client.cancel_open_stop_orders(ib, contract)
    if cancelled:
        logger.info(f"Cancelled {cancelled} existing protective stop order(s) for "
                    f"{contract_label} before this add")

    logger.info(f"[trade_executor] Ready to fire, Dracarys... BUY x{qty} {contract_label}")
    trade = ibkr_client.place_order(ib, contract, "BUY", qty, price_type)
    outcome = (f"ADD placed: BUY x{qty} (of {held_qty} held, {pct}%) {contract_label} "
               f"ibkr_order_id={trade.order.orderId}")
    logger.info(outcome)
    db.insert_order(signal.db_id, "signal", "BUY", contract.symbol, contract_label, qty,
                     price_type, ibkr_order_id=trade.order.orderId, status="submitted",
                     detail=outcome)

    if cancelled <= 0:
        # no prior stop to replace — that stays TRIM's job, ADD doesn't
        # originate protection from nothing.
        db.update_signal_outcome(signal.db_id, outcome, contract_label, failed=False)
        return trade

    # read the position back live rather than computing the blended cost
    # ourselves — this bot never waits for a fill, so immediately after
    # placeOrder() the true post-add avg cost may or may not exist yet; the
    # short sleep-then-read (same trick used elsewhere in ibkr_client) picks
    # it up if the buy has already filled. Matched by conId, not symbol —
    # if this ticker has another distinct contract open too, a symbol-only
    # match could read back the wrong one's qty/avg cost and size the
    # replacement stop for the wrong strike.
    matches = [(q, a) for c, q, a in ibkr_client.get_open_option_positions(ib)
               if c.conId == contract.conId]
    new_qty, new_avg_premium = matches[0] if matches else (None, None)
    new_qty = abs(int(new_qty)) if new_qty else held_qty + qty
    expected_qty = held_qty + qty
    if new_qty < expected_qty:
        logger.info(f"This add hasn't filled yet as far as IBKR's position report shows "
                    f"({new_qty} of an expected {expected_qty} for {contract_label}) — the "
                    f"replacement stop below only covers what's currently confirmed and won't "
                    f"automatically resize once the add fills.")

    if new_avg_premium is None:
        logger.info(f"No avg entry price available for {contract_label} — skipping "
                     f"replacement protective stop after this add")
        db.update_signal_outcome(signal.db_id, outcome, contract_label, failed=False)
        return trade

    stop_trade = ibkr_client.place_stop_order(ib, contract, "SELL", new_qty, new_avg_premium)
    stop_outcome = (f"Protective stop replaced: STP SELL x{new_qty} {contract_label} "
                     f"@ {stop_trade.order.auxPrice} (avg entry) "
                     f"ibkr_order_id={stop_trade.order.orderId}")
    logger.info(stop_outcome)
    db.insert_order(signal.db_id, "signal", "STP SELL", contract.symbol, contract_label, new_qty,
                     price_type, ibkr_order_id=stop_trade.order.orderId, status="submitted",
                     detail=stop_outcome)
    db.update_signal_outcome(signal.db_id, f"{outcome} {stop_outcome}", contract_label,
                              failed=False)
    return trade


def load_risk_config(config_path):
    """Re-reads the risk section of config.yaml fresh off disk. Called right
    before every handled signal (see run_worker) so a live edit — flipping
    require_confirmation, changing price_type/strike_offset/allowed_tickers,
    etc. — takes effect on the very next signal instead of needing a
    restart."""
    with open(config_path) as f:
        config = yaml.safe_load(f)
    return config["risk"]


def load_reconnect_config(config_path):
    """Re-reads the reconnect section of config.yaml fresh off disk, same
    per-signal-fresh convention as load_risk_config. Missing section (older
    config.yaml, or the key just omitted) defaults to retry_on_reconnect=True
    and no timeout — i.e. the pre-existing behavior of always eventually
    placing a signal that arrived during an IBKR outage, never silently
    dropping it. That default matches this bot's risk philosophy (false
    negatives — a signal that should have fired but didn't — are worse than
    false positives); an explicit config.yaml entry is required to opt into
    discarding stale signals instead."""
    with open(config_path) as f:
        config = yaml.safe_load(f)
    return config.get("reconnect") or {}


def _reconnect_skip_reason(config_path, signal, reconnected_at):
    """None if a signal that was still waiting when IBKR reconnected (see
    run_worker) should go ahead and be placed now, else a string explaining
    why it shouldn't. retry_on_reconnect=false skips every such signal
    outright; retry_timeout_mins caps how long the wait is allowed to have
    been, measured from signal.received_at (when the message was classified)
    to reconnected_at — per the reconnect.retry_timeout_mins example in
    config.yaml, this is elapsed time since the signal *arrived*, not since
    IBKR went down, since a signal can arrive at any point mid-outage."""
    reconnect_cfg = load_reconnect_config(config_path)
    if not reconnect_cfg.get("retry_on_reconnect", True):
        return "IBKR was disconnected when this signal arrived and reconnect.retry_on_reconnect is false"
    timeout_mins = reconnect_cfg.get("retry_timeout_mins")
    elapsed = reconnected_at - signal.received_at
    if timeout_mins is not None and elapsed > timeout_mins * 60:
        return (f"IBKR reconnected {elapsed / 60:.1f}min after this signal arrived, past "
                f"reconnect.retry_timeout_mins ({timeout_mins}min)")
    return None


def _drain_validation_queue(ib, validation_queue, logger):
    """Services every pending "+ add ticker" request from the web UI (see
    run_worker's docstring) — there's essentially never more than one
    queued at a time, but drains all of them rather than just one in case
    a burst arrives, same posture as signal_queue."""
    while True:
        try:
            req = validation_queue.get_nowait()
        except queue.Empty:
            return
        try:
            req.valid, req.reason = ibkr_client.validate_ticker(ib, req.ticker)
        except Exception as e:
            req.valid, req.reason = False, f"Validation error: {e}"
            logger.exception(f"Error validating ticker {req.ticker!r}")
        req.event.set()


def _snapshot_state(ib, logger):
    """Refreshes db.positions_snapshot from live IBKR state. This is the
    only bridge between ib_async (worker-thread-only) and the web UI's
    Flask thread (db-only) — see web/server.py's docstring. Best-effort:
    a snapshot failure logs and moves on rather than taking down the
    worker loop, same posture as the reqPositions() call in run_worker."""
    try:
        positions = ibkr_client.get_open_option_positions(ib)
        rows = []
        for contract, qty, avg_premium in positions:
            contract_label = (f"{contract.symbol} {contract.lastTradeDateOrContractMonth} "
                               f"{contract.strike}{contract.right}")
            last = ibkr_client.get_last_price(ib, contract)
            multiplier = float(contract.multiplier or 100)
            unrealized = None
            if avg_premium is not None and last is not None:
                unrealized = (last - avg_premium) * abs(qty) * multiplier
            stops = ibkr_client.count_open_stop_orders(ib, contract)
            stop_desc = (f"{stops} protective stop order(s) working" if stops
                         else "No protective stop")
            rows.append({
                "ticker": contract.symbol, "contract_label": contract_label, "qty": int(qty),
                "avg": avg_premium, "last": last, "unrealized_pnl": unrealized,
                "stop_desc": stop_desc,
            })
        db.snapshot_positions(rows)
        db.set_bot_state("ibkr_last_snapshot_ts", time.time())
    except Exception:
        logger.exception("Failed to snapshot positions for the web UI (non-fatal)")


def run_worker(config_path, config, signal_queue, validation_queue, logger, stop_event):
    """Runs in its own thread with its own event loop — ib_async's standard
    single-process usage (same as IBKRStocks' ibkr_order_placer.py), kept
    separate from the discord.py-self listener's asyncio loop rather than
    sharing one, since combining two loop-owning async libraries in one
    thread is exactly the kind of integration that's easy to get subtly
    wrong and hard to verify without a live Gateway to test against.
    A fresh (non-main) thread has no event loop by default — ib_async needs
    one, hence the explicit new_event_loop/set_event_loop below. ib_async
    is imported here, not at module level, so the handle_*/_resolve_*
    functions above stay importable/testable without it installed.

    config is the already-loaded dict (used once, for the ibkr section —
    reconnecting live on an edit isn't handled here); config_path is kept
    around to reload just the risk section per-signal via load_risk_config.

    validation_queue carries ticker-validation requests from the web UI's
    "+ add ticker" flow (web/server.py's TickerValidationRequest — duck-typed
    here, no import needed: just .ticker in, .valid/.reason out, then
    .event.set()) — routed through this thread for the same reason
    signal_queue is: only this thread may ever touch ib_async. Drained on
    every idle tick, after signal_queue, so a burst of trade signals is
    never delayed behind a ticker-validation request."""
    from ib_async import IB

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    ibkr_cfg = config["ibkr"]
    risk_cfg = config["risk"]  # last-known-good; refreshed before every signal below

    ib = IB()
    ibkr_client.connect_ibkr(ib, ibkr_cfg["host"], ibkr_cfg["port"], ibkr_cfg["client_id"])
    try:
        ib.reqPositions()  # subscribe to live position updates, used by get_open_option_position(s)
    except Exception:
        logger.exception("reqPositions failed (non-fatal)")
    ibkr_client.track_daily_pnl(ib)  # feeds max_daily_losing_trades / daily_loss_limit in handle_entry/handle_add

    handlers = {
        SignalType.ENTRY: handle_entry,
        SignalType.EXIT: handle_exit,
        SignalType.TRIM: handle_trim,
        SignalType.ADD: handle_add,
    }

    last_snapshot = 0.0
    # ~matches the web UI's own poll cadence (app.js) — no point refreshing
    # db.positions_snapshot faster than anything will ever read it.
    snapshot_interval = 2.5

    # Wall-clock time the most recent mid-run reconnect finished, or None if
    # IBKR hasn't dropped since this worker started. Never reset back to
    # None afterward — a signal's received_at only needs comparing against
    # it once (see the reconnect.* check below), and every signal classified
    # after that moment necessarily has a later received_at, so the
    # comparison naturally stops matching on its own once the outage's
    # backlog has drained. Deliberately NOT "was ib.isConnected() False when
    # this exact signal was popped" — with a backlog of several signals
    # queued during one outage, only the first one pulled after the drop
    # would still observe the disconnected state; the rest would already see
    # a freshly-reconnected ib and skip the check entirely, even though they
    # arrived during the very same outage.
    reconnected_at = None

    while not stop_event.is_set():
        if not ib.isConnected():
            # connect_ibkr (inside ensure_connected) retries indefinitely, so
            # this blocks the whole loop — including signal_queue draining —
            # until IBKR is back. Checked at the top of every iteration
            # (not just the idle branch below) so a signal that's next in
            # line gets caught by this before it's ever handed to a handler.
            outage_started = time.time()
            ibkr_client.ensure_connected(ib, ibkr_cfg["host"], ibkr_cfg["port"], ibkr_cfg["client_id"])
            reconnected_at = time.time()
            logger.info(f"[trade_executor] IBKR outage lasted "
                        f"{reconnected_at - outage_started:.0f}s")

        try:
            signal = signal_queue.get_nowait()
        except queue.Empty:
            # ib_async is pure-asyncio with no background reader thread —
            # placeOrder/cancelOrder just write to the socket and return, so
            # fills, rejections, and order-status changes only get dispatched
            # to statusEvent/wrapper callbacks when something drives the
            # event loop (ib.sleep does this; queue.get(timeout=...) does
            # not). Without this, ENTRY signals and idle periods never
            # process incoming IBKR messages at all — a fill or a rejection
            # could sit unseen for as long as it takes the next TRIM/EXIT/ADD
            # signal to arrive and call get_open_option_positions (which
            # pumps the loop itself), or indefinitely if none ever does.
            ib.sleep(1)
            _drain_validation_queue(ib, validation_queue, logger)
            ibkr_client.flush_pending_round_trips()
            now = time.monotonic()
            if now - last_snapshot >= snapshot_interval:
                _snapshot_state(ib, logger)
                last_snapshot = now
            continue

        handler = handlers.get(signal.type)
        if handler is None:
            continue

        if reconnected_at is not None and signal.received_at < reconnected_at:
            skip_reason = _reconnect_skip_reason(config_path, signal, reconnected_at)
            if skip_reason:
                msg = (f"SKIP {signal.type.value} {signal.ticker or ''}: {skip_reason} — "
                       f"discarding (raw: {signal.raw_text!r})")
                logger.info(msg)
                db.update_signal_outcome(signal.db_id, msg, signal.ticker, failed=True)
                _snapshot_state(ib, logger)
                last_snapshot = time.monotonic()
                continue

        try:
            risk_cfg = load_risk_config(config_path)
        except Exception:
            logger.exception(f"Failed to reload config.yaml before handling {signal.type.value} "
                              f"signal — using last-known-good risk settings instead")
        try:
            handler(ib, signal, risk_cfg, logger)
        except Exception:
            logger.exception(f"Error handling {signal.type.value} signal: {signal}")
            db.update_signal_outcome(signal.db_id, f"Error handling {signal.type.value} signal "
                                      f"(see casey_bot.log)", signal.ticker, failed=True)
        # refresh immediately after anything that could have changed
        # positions, rather than waiting for the next idle tick — keeps the
        # UI's Positions screen snappy right after a fill.
        _snapshot_state(ib, logger)
        last_snapshot = time.monotonic()

    ib.disconnect()
