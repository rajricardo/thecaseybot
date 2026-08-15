# thecaseybot
Reads Casey's Discord, places IBKR options orders accordingly

## What it does

- Watches a specific Discord channel/author for trading calls (`discord_listener.py`).
- Classifies each message as ENTRY / EXIT / TRIM / ADD / NOISE — fast regex first
  (`signal_classifier.py`), falling back to Claude (`llm_classifier.py`) for anything
  the regex can't confidently place.
- Turns a classified signal into a real (or dry-run) Interactive Brokers options order,
  risk-gated by `config.yaml`'s `risk` section (`trade_executor.py` + `ibkr_client.py`).
- Serves a local control UI, **Casey Bridge**, at `http://127.0.0.1:8787` — live
  positions, the signal feed, order history, and a Settings screen that edits
  `config.yaml` directly (`web/server.py`, `web/static/`).

## Requirements

- Python 3.9+ (uses `zoneinfo`, no extra tzdata package needed on macOS/Linux)
- [Interactive Brokers TWS or IB Gateway](https://www.interactivebrokers.com/en/trading/tws.php)
  running locally with the API enabled — **paper trading account strongly recommended
  to start**, since this bot places real orders once configured to
- A Discord **personal account token** (self-bot mode, not a bot-account token) for the
  account that can see the channel you want to watch — see `discord_listener.py`'s
  docstring for how this works and the account-risk/ToS tradeoff of self-botting
- An [Anthropic API key](https://console.anthropic.com/) — used only for the
  small fraction of messages the regex classifier can't confidently place

## Setup

```bash
git clone https://github.com/rajricardo/thecaseybot.git
cd thecaseybot
cp config.example.yaml config.yaml
```

Edit `config.yaml`:

- `discord.user_token` — your Discord account's session token (see
  [Getting your Discord token](#getting-your-discord-token) below)
- `discord.channel_id` / `discord.casey_user_id` — the channel to watch and the
  numeric ID of the author whose messages count as signals
- `llm.api_key` — your Anthropic API key
- `ibkr.host` / `ibkr.port` / `ibkr.client_id` — where TWS/Gateway is listening
  (4001 = paper Gateway, 4002 = live Gateway, 7497/7496 = TWS) — see
  [IBKR TWS/Gateway API setup](#ibkr-twsgateway-api-setup) below
- `risk.*` — see below; everything here is also editable live from the Settings
  screen once the bot is running, no restart needed (Discord/IBKR/LLM settings
  do need a restart)

### Getting your Discord token

`discord.user_token` is your own Discord account's session token, not a
Developer Portal bot token — this is self-bot mode (see `discord_listener.py`'s
docstring for the mechanics and the ToS/account-risk tradeoff of automating a
personal account). To grab it:

1. Open Discord in a desktop browser (not the desktop app) and log in.
2. Open DevTools (`Cmd+Option+I` on macOS, `F12`/`Ctrl+Shift+I` on
   Windows/Linux) and switch to the **Network** tab.
3. Click into any server/channel in Discord's UI so a new request fires, then
   filter the Network tab for `api` and click one of the requests to
   `discord.com/api/...`.
4. In that request's **Headers**, find `authorization:` under Request Headers
   — the value is your token. Copy it exactly (no `Bearer ` prefix) into
   `discord.user_token`.
5. Also grab `discord.channel_id` (right-click the channel → Copy Channel ID)
   and `discord.casey_user_id` (right-click the author's name → Copy User ID)
   — both require Discord's Developer Mode enabled (User Settings → Advanced
   → Developer Mode) to see the "Copy ID" option.

Treat this token exactly like a password: anyone with it can act as your
Discord account. Never commit it (`config.yaml` is gitignored for this
reason) and never paste it into a notes file that might end up somewhere else.

### IBKR TWS/Gateway API setup

The bot connects to TWS or IB Gateway over its local API socket, so that has
to be turned on and unattended-friendly before `bot.py` can place orders:

1. Open TWS or IB Gateway and log in (paper account recommended to start —
   [IB Gateway](https://www.interactivebrokers.com/en/trading/ibgateway-stable.php)
   is the lighter-weight headless option if you don't need the full TWS UI).
2. Go to **File → Global Configuration → API → Settings** (TWS) or
   **Configure → Settings → API → Settings** (Gateway).
3. Check **Enable ActiveX and Socket Clients**.
4. Set the **Socket port** to match `ibkr.port` in `config.yaml` (4001 for
   paper Gateway, 4002 for live Gateway, 7497/7496 for paper/live TWS).
5. **Uncheck "Read-Only API"** — this must be off, or every order the bot
   submits gets silently rejected by TWS/Gateway itself.
6. Add `127.0.0.1` under **Trusted IP Addresses** (or check "Allow connections
   from localhost only" if that's all you need) so the bot's own machine can
   connect without an approval popup per session.
7. Check **"Bypass Order Precautions for API Orders"** in the same API
   settings page. Without this, TWS pops up a confirmation dialog for things
   like a large order size or a price far from the market — dialogs nobody's
   there to click through when the bot is running unattended, so the order
   just hangs. This is the "safety check" toggle — bypassing it here still
   leaves `risk.require_confirmation` in `config.yaml` as the bot's own
   pre-submission gate.
8. Make sure `ibkr.client_id` in `config.yaml` isn't already in use by
   another API connection (TWS itself, another script, a second run of this
   bot) — two connections sharing a client ID will fight each other.

Then either run `./run.sh` (creates the venv, installs `requirements.txt`, and starts
the bot for you), or do it by hand:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python bot.py
```

Once running, open `http://127.0.0.1:8787` for the Casey Bridge UI.

**Start with `risk.require_confirmation: true`** (the default in
`config.example.yaml`) — signals are classified and logged, but no order is ever
submitted to IBKR. Flip it to `false` only once you've watched it behave correctly
against a paper account.

## `risk` config reference

| Key | Meaning |
|---|---|
| `allowed_tickers` | Only ENTRY signals for these tickers are acted on |
| `sizing_mode` | `fixed` — every entry uses `max_contracts_per_trade` contracts. `dynamic` — contracts are derived from `capital_per_trade` and the contract's live LAST price: `floor(capital_per_trade / (LAST price * 100))` |
| `max_contracts_per_trade` | Contracts per entry when `sizing_mode` is `fixed` |
| `capital_per_trade` | Dollars to deploy per entry when `sizing_mode` is `dynamic` |
| `max_concurrent_positions` | Blocks new entries once this many distinct tickers are open |
| `max_daily_losing_trades` | Blocks new ENTRY/ADD once this many trades close as losers today (TRIM/EXIT are never blocked) |
| `daily_loss_limit` | Same, but keyed off today's realized P&L in dollars instead of a trade count; `null` disables it |
| `strike_offset` | `<rank>ITM` \| `<rank>OTM`, e.g. `1ITM`, `2OTM` — there's no ATM, use `1OTM` for closest-to-money |
| `expiry_selection` | `nearest` (0DTE if available) or `weeklies` (next Friday) |
| `price_type` | `MARKET` \| `MIDPOINT` \| `BID` \| `ASK` \| `LAST` \| `AUTO`. `AUTO` submits `MIDPOINT` before 9:45am ET and switches to `MARKET` from 9:45am ET on, since IBKR doesn't accept MARKET orders on options right at the open |
| `require_confirmation` | `true` = log the intended order but never submit it. `false` = actually place it |
| `trim_pct` | % of the *currently held* quantity sold on every TRIM signal (applied fresh each time) |
| `auto_submit_stop_loss` | After a TRIM, place a protective stop on the remaining runners at the average entry price |
| `add_pct` | % of the *currently held* quantity bought on every ADD signal |

`reconnect.*` controls what happens to a signal that arrived while IBKR was
disconnected, once the connection comes back — see the comments in
`config.example.yaml`.

## Safety notes

- This bot places real brokerage orders. Test against an IBKR **paper** account
  (port 4001) with `require_confirmation: true` before ever pointing it at a live
  account.
- `config.yaml` holds real secrets (Discord token, Anthropic key) and is
  gitignored — never commit it. Only `config.example.yaml` (placeholder values)
  is tracked.
- The Casey Bridge web UI binds to `127.0.0.1` only and is never reachable off
  the machine it runs on.
