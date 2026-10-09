# prediction-market

A news-driven trading research bot for Kalshi and Polymarket. It reads headlines, matches them to open markets, asks a
decision model one typed question per candidate market, applies a fixed trade rule, and simulates a fill against the
live order book. **It always starts on paper.** Real Kalshi orders are opt-in: you enable them in `.env`, then flip a
switch in the dashboard each session (see [Real trading](#real-trading-opt-in)). The only order code is one
immediate-or-cancel buy in `fastlane/live.py`, and a test enforces that.

> **Disclaimer.** This is educational and research software. It is not financial advice. Paper results do not predict
> real results, and if you turn real trading on you can lose the money you trade. It comes with no warranty (see
> AGPL-3.0 sections 15 and 16). You are responsible for checking the Kalshi and Polymarket terms of service and your
> own eligibility in your jurisdiction. Do not use VPNs or any other means to evade geo-restrictions. Prediction
> markets may be restricted or illegal where you live.

Requires Python >= 3.11.

## Architecture

```
news: 18 RSS feeds + SEC 8-K  |  X (10 handles via xAI x_search, 60 s)  |  Bluesky newsrooms (Jetstream)  |  Kalshi live tape -> move detector
  -> market match (IDF index over ~31k open markets, <1 ms)
  -> Jev decision (one call, one question per candidate market) + order books prefetched in parallel
  -> fixed trade rule (decision.py) + freshness guards (stale news, already priced in)
  -> paper fill walking the live order book, with Kalshi taker fees
  -> price marks at +5s, 30s, 60s, 5m, 15m, 1h
```

| File | Role |
|---|---|
| `fastlane/config.py` | Repo root, results directory, `.env` loading. |
| `fastlane/feeds.py` | Async pollers with conditional GETs. First poll of each feed is backlog and never traded. Includes the unofficial Trump archive feed. |
| `fastlane/x_feed.py` | X posts from a fixed handle group through xAI's x_search; one Grok call per poll, windowed and budget-capped. |
| `fastlane/bluesky.py` | Bluesky newsroom accounts over Jetstream, with a getAuthorFeed polling fallback. |
| `fastlane/universe.py` | Loads open Kalshi (non-sports) and top Polymarket markets, builds the match index. |
| `fastlane/jev_client.py` | Pooled HTTP/2 client for the OpenRouter Decisions API, pinned to `typesafe/jev-1.13`. |
| `fastlane/decision.py` | The per-market Jev questions, the fixed trade thresholds, the shadow rule and cost filter settings. |
| `fastlane/books.py` | Order books normalised to ask ladders, book-walking fill, Kalshi fee formula. |
| `fastlane/engine.py` | Hot path, risk checks, keep-warm pings, price marks. |
| `fastlane/ledger.py` | SQLite tables: events, decisions (per-stage timings), trades, marks. |
| `fastlane/kalshi.py` | Signs the Kalshi WebSocket handshake (RSA-PSS or Ed25519); read-only. |
| `fastlane/kalshi_tape.py` | Live Kalshi quotes: price-at-publish-time, first reaction time, and the move detector. |
| `fastlane/report.py` | Stage latencies, source lag, decisions, market moves after news, mark-to-bid P&L. |
| `fastlane/bench_jev.py` | Standalone Jev benchmark. |
| `fastlane/api.py` | FastAPI over the ledger: read-only `GET` routes plus two guarded writes, `POST /settings/shadow` (shadow toggle) and `POST /control/mode` (the paper/real switch). |
| `fastlane/static/index.html` | Single-file terminal dashboard served at `/` (light and dark, keyboard driven) with the TRADING MODE panel, no external requests. |
| `fastlane/live.py` | Real Kalshi orders: off by default, paper after every restart, hard caps, IOC buys only. |
| `fastlane/ratelimit.py` | Per-client API rate limits and the Jev call/spend budget. |
| `fastlane/errors.py` | Local error log plus scrubbed crash reports (Sentry). |
| `fastlane/backup.py` | Ledger backups, restore and the restore drill. |
| `fastlane/deploy.py`, `hosted.py` | Password-protected read-only copy of the dashboard on your own Vercel account. |

## Quick start

```bash
git clone https://github.com/mehek-builds/prediction-market && cd prediction-market
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in OPENROUTER_API_KEY at minimum
python3 -m fastlane.run --inject "Fed cuts rates by 50 bps" --minutes 1   # synthetic end-to-end test
python3 -m fastlane.run                                                   # live, Ctrl-C to stop
python3 -m uvicorn fastlane.api:app --port 8787                           # in a second terminal
open http://localhost:8787
```

Environment variables (see `.env.example`):

| Variable | Needed | What it does |
|---|---|---|
| `OPENROUTER_API_KEY` | required | OpenRouter key used for the Jev decision model (Decisions API). |
| `JEV_MODEL` | optional | Model id sent to OpenRouter. Default pins the version the trade rule was tuned on. |
| `KALSHI_API_KEY_ID` | optional | Kalshi API key id for the live WebSocket tape. |
| `KALSHI_PRIVATE_KEY_PATH` | optional | Path to the PEM file, OR the full PEM text, OR the bare base64 body of a PKCS#8 key (RSA or Ed25519). |
| `SEC_USER_AGENT` | optional | Contact string for the SEC EDGAR poller, format `Name email@example.com` (SEC fair-access policy). |
| `PAPER_BANKROLL_USD` | optional | Paper bankroll, default 10000. |
| `PAPER_MAX_TRADE_PCT` | optional | Fraction of bankroll per trade, default 0.02. |
| `PAPER_DAILY_LOSS_HALT_PCT` | optional | Daily loss halt as a fraction of bankroll, default 0.05. |
| `MAX_SPREAD_CENTS` | optional | Cost filter: skip a market whose held-side spread (ask minus bid) is over this many cents at decision. Default 3. |
| `COST_TO_ROOM_MAX` | optional | Cost filter: skip when spread plus both taker fees exceed this fraction of the room to profit (1 minus entry). Default 0.25. |
| `SHADOW_ENABLED` | optional | Record a shadow book of what a looser rule would have traded, default true (the dashboard toggle, stored in the ledger, overrides this). |
| `SHADOW_SIGNAL_THRESHOLD` | optional | Shadow rule signal threshold, default 0.60 (real rule: 0.85, unchanged). |
| `SHADOW_DECISIVE_MIN` | optional | Shadow rule decisive minimum, default 0.0 (real rule: 0.30, unchanged). |
| `XAI_API_KEY` | optional | xAI key. Enables the X poller; about $0.018 to $0.055 per call (measured). Off when empty. |
| `XAI_X_HANDLES` | optional | Up to 10 X handles, comma separated, without @. Default: DeItaone, FirstSquawk, LiveSquawk, zerohedge, unusual_whales, Polymarket, Breaking911, KobeissiLetter, WhiteHouse, truthsocial. |
| `XAI_POLL_SECONDS` | optional | Seconds between calls inside the window, min 15, default 60. |
| `XAI_DAILY_BUDGET_USD` | optional | Hard cap per UTC day from the API's own cost field, persisted in the ledger; polling stops for the day when reached. Default 25. |
| `XAI_WINDOW_DAYS` / `XAI_WINDOW_HOURS` / `XAI_WINDOW_TZ` | optional | Active window, default Mon-Fri 09:00-16:30 America/New_York. |
| `BSKY_ENABLED` | optional | Bluesky Jetstream consumer, keyless, default true. |
| `BSKY_HANDLES` | optional | Comma separated Bluesky handles, default the ten newsroom accounts in `.env.example`. |
| `MIN_ENTRY_PRICE` | optional | Cost filter: skip entries below this price (long shots), default 0.03. |
| `MOVE_DENY_RE` | optional | Regex (case-insensitive) matched against a Kalshi market's title and ticker; matching markets never fire the move detector. Default excludes gas price and other price-on-a-date ladders. |
| `FASTLANE_ALLOWED_HOSTS` | optional | Extra Host header values the dashboard API accepts (comma separated). `localhost` and `127.0.0.1` are always allowed; other Hosts get HTTP 400 (DNS rebinding guard). Never expose the API port beyond this machine (`--host 0.0.0.0` without the `127.0.0.1:` Docker mapping, port forwarding, tunnels) while `LIVE_TRADING_ENABLED=1`: the Host check only protects browsers and does not stop other machines, and anyone who can reach the port can arm real trading within the `LIVE_MAX_*` caps. |
| `LIVE_TRADING_ENABLED` | optional | `1` allows the dashboard's real-money switch. Off by default. See [Real trading](#real-trading-opt-in). |
| `LIVE_MAX_ORDER_USD` | optional | Real trading: most one order may cost, fees included. Default 5. |
| `LIVE_MAX_DAILY_USD` | optional | Real trading: most spent in any 24 hours. Default 25. |
| `LIVE_MAX_ORDERS_PER_HOUR` | optional | Real trading: order rate cap. Default 6. |
| `LIVE_ALLOW_NO_SIDE` | optional | Default `0`: real orders are only sent for YES buys, and a NO signal stays a paper trade (logged "real NO orders disabled until verified"). Kalshi's V2 docs describe a YES-book ask as economically equivalent to buying NO but do not state that an ask with no YES position opens NO. Verify on Kalshi's demo environment before setting `1` (procedure below). |
| `LIVE_ALLOW_REMOTE_CONTROL` | optional | DANGEROUS, default `0`. The token (`/control/state`) and arming (`/control/mode` with `mode: live`) only work from a loopback client (127.0.0.1 or ::1). `1` lifts that (needed to arm from a Docker API container; keep the `127.0.0.1:` port mapping, and note other containers on the same Docker network can then reach it). Leave it off. |
| `KALSHI_BASE_URL` | optional | Host for every Kalshi call (orders, balance, market data, WebSocket). Default `https://api.elections.kalshi.com`. The only other accepted value is the demo host `https://demo-api.kalshi.co` (WebSocket becomes `wss://demo-api.kalshi.co/trade-api/ws/v2`); anything else stops startup. Demo needs its own demo API key, and keeps its data in its own folder, `fastlane/results-demo/`. |
| `JEV_MAX_CALLS_PER_HOUR` | optional | Jev calls allowed per rolling hour; over it, headlines are logged as `PASS jev_hourly_cap`. Default 600. `0` turns the cap off. |
| `JEV_MAX_USD_PER_DAY` | optional | Jev spend allowed per rolling 24 h (from OpenRouter's reported cost); over it, `PASS jev_daily_spend_cap`. Default 5. |
| `API_RATE_LIMIT_PER_MIN` | optional | Dashboard API requests per minute per client (HTTP 429 beyond). Default 120, burst `API_RATE_LIMIT_BURST` 60. |
| `BACKUP_EVERY_HOURS` | optional | Engine backs the ledger up this often, and on clean shutdown. Default 6, `0` turns scheduled backups off. |
| `BACKUP_DIR` | optional | Where backups go. Default `fastlane/results/backups`. Point it at a synced folder to survive a dead disk. |
| `BACKUP_KEEP` | optional | Backups kept. Default 28. |
| `FASTLANE_TELEMETRY` | optional | Default `0`: nothing is sent. `1` shares crash reports with the maintainer. See [Error reports](#error-reports). |
| `FASTLANE_SENTRY_DSN` | optional | Send crash reports to your own Sentry project instead. |

What is disabled when the optional ones are empty:

- No Kalshi key (or only one of the two Kalshi variables): the live tape is off. That removes the move detector,
  the price-at-publish lookup and the priced-in guard. Public Kalshi market data and order books still work.
- No `SEC_USER_AGENT`: the EDGAR 8-K poller is off. The RSS feeds still run.
- No `XAI_API_KEY`: the X poller is off; nothing is billed.
- `BSKY_ENABLED=false`: no Bluesky.

## Commands

```bash
python3 -m fastlane.run                          # live until Ctrl-C
python3 -m fastlane.run --minutes 30 --workers 8 # live for 30 minutes, 8 decision workers
python3 -m fastlane.run --inject "headline"      # synthetic headline (repeatable), live feeds off
python3 -m fastlane.report                       # timeline + P&L from the ledger
python3 -m fastlane.report --since-minutes 60
python3 -m fastlane.bench_jev --n 200 --repeats 3 --concurrency 4   # Jev latency/stability benchmark
python3 -m fastlane.bench_jev --dry-run          # sources only, no Jev calls, no key needed
python3 -m uvicorn fastlane.api:app --port 8787  # dashboard and JSON API
python3 -m pytest -q                             # tests (offline, no keys)
python3 -m fastlane.backup                       # back up the ledger now (--list, --verify, --restore PATH --yes)
python3 -m fastlane.deploy                       # put a password-protected copy of the dashboard on Vercel
python3 -m fastlane.run --vercel                 # run the engine and keep the Vercel copy in sync
python3 -m fastlane.live paper                   # kill switch: back to paper right now
```

Docker (the Dockerfile header has the same commands):

```bash
docker build -t fastlane .
docker run -d --restart unless-stopped --env-file .env -v fastlane-results:/app/fastlane/results fastlane
docker run -p 127.0.0.1:8787:8787 --env-file .env -v fastlane-results:/app/fastlane/results fastlane \
  python3 -m uvicorn fastlane.api:app --host 0.0.0.0 --port 8787
```

Demo data lives in `fastlane/results-demo/`, outside that volume. If you run the demo host in Docker, mount its own
volume too (`-v fastlane-results-demo:/app/fastlane/results-demo`) on both the engine and API containers, or it is lost
on restart. Publishing to Vercel is refused while `KALSHI_BASE_URL` is the demo host.

Arming real money from the Docker API container does not work by default: the container sees the Docker gateway
address, not loopback, so `/control/state` gives no token and the dashboard shows "Arming real money only works from
this machine's browser" with the switch disabled. Either run the API outside Docker (`python3 -m uvicorn fastlane.api:app`)
to arm, or set `LIVE_ALLOW_REMOTE_CONTROL=1` together with the `127.0.0.1:` port mapping (other containers on the same
Docker network can then reach the API and arm). Never set uvicorn's `FORWARDED_ALLOW_IPS` / `--forwarded-allow-ips` to
`*` while `LIVE_TRADING_ENABLED=1`: uvicorn would trust a client-supplied X-Forwarded-For and any client could claim
127.0.0.1.

`--env-file` cannot hold multi-line values, so put the Kalshi key body on one line or mount a key file.

## Dashboard

`python3 -m uvicorn fastlane.api:app --port 8787` serves one static page at `/`: no build step, no external requests.

- Masthead: a persistent `PAPER` badge (it reads `LIVE MONEY`, in red with a double rule, while real trading is armed, or `DEMO` when armed against the demo host) and status cells for the run state, the Kalshi tape, the feeds (X calls and
  spend against the daily budget, Bluesky websocket or polling, feeds with events in the last hour), the shadow switch
  and data age, a `MODE` cell (`PAPER · REAL OFF`, `PAPER · REAL ALLOWED`, `LIVE MONEY · $x OF $y TODAY` or
  `READ-ONLY COPY`), plus ET and UTC clocks.
- BOOKS: LIVE, SHADOW and TEST rows with a totals row for the real book. The `LIVE` row is the paper book under the
  real rule; real money is only ever called `LIVE MONEY`.
- TRADING MODE: the paper/real switch (`SWITCH TO LIVE MONEY`, the terms, a typed `TRADE REAL MONEY`, then `ARM LIVE
  MONEY`; `BACK TO PAPER` once armed), the last error, and the table of real orders. No key arms real money. The
  Vercel copy has no switches (the `H` cap is hidden); it shows the real-orders table from the snapshot.
- BLOTTER: every trade, sortable by column header, as a table or as cards.
- P&L 24H: mark-to-bid curve per book.
- LATENCY: p50, p90 and last for match, Jev, book and total.
- SHADOW BY SIGNAL: shadow P&L per signal-strength bucket.
- NEWS / DECISIONS: every headline judged, with the verdict and how the market moved afterwards.

Keys: `1` all, `2` live, `3` shadow, `4` test books; `B` table, `C` cards; `S` cycle sort; `H` toggle shadow mode;
`T` toggle theme; `R` refresh; `?` help. Themes are TERMINAL (dark) and LEDGER (light). The page follows
`prefers-color-scheme`; `T` overrides it and is stored in `localStorage` under `fastlane.theme`; `?theme=light|dark`
is a one-shot override for screenshots. `GET /status` is read-only and derived from the ledger timestamps, so it says
"last tick 12s ago", never "up" or "down". `GET /settings` reports the shadow switch and its source, and `/decisions`
rows carry `shortlist_ms`, `book_ms` and `n_candidates`.

## Fast sources

Why: on 5 stories covered by both, our RSS feeds saw the story a median of about 32 minutes after the first X post
(range -5 to +109 minutes). The first reporters were wire and squawk accounts on X. The engine decides in about 400 ms;
the feeds are the bottleneck. Three faster sources feed the same event queue. They only produce headlines (no source
places orders), and post text is treated as untrusted data (it is stored and shown as a headline, never used as an instruction or a
query).

**X via xAI.** Each poll is one Grok call with the `x_search` tool over the handle group: one `x_keyword_search`, at
most about 10 posts, typically 4 to 15 seconds, about $0.018 to $0.055. `max_tool_calls: 1` stops Grok from opening threads at triple the
cost. A returned post is accepted only if its URL handle is in the allowed list, its snowflake id decodes to a time
inside the poll window (Grok can answer from memory when the search is empty), and the id is new; rejections are
counted. Calls happen only inside the active window (default Mon-Fri 09:00-16:30 New York) and under the daily
budget, which is read from the ledger so a restart does not reset it. At 60 calls an hour a full default window
(7.5 hours) is 450 calls: about $8 to $25 at the measured cost. The $25 cap only binds at the high end; the window
is the usual limit. The first poll of each window (and the first after a
budget stop or a long backoff) is backlog: seen, never traded.

**Bluesky.** Newsroom accounts over the keyless Jetstream firehose, own top-level posts only (replies and reposts are
skipped). If the socket fails, `getAuthorFeed` is polled every 15 s for 5 minutes, then the socket is retried. Posts
missed during a reconnect gap are lost on purpose (replaying would be backlog). `createdAt` is clamped to the receipt
time.

**Trump.** His X account is near-dormant. Truth Social blocks automated access and is never fetched.
`trumpstruth.org` is an UNOFFICIAL third-party archive, polled every 10 s as an ordinary RSS feed, and `@truthsocial`
is in the default X group.

Expected lag per source (the source-lag section of `python3 -m fastlane.report` shows yours): Bluesky seconds, X about
half a poll interval plus the call, RSS minutes. `GET /health` reports X calls and spend today, the budget and window
flags, and the Bluesky connection state.

## Trade rule and guards

One Jev question per candidate market (up to 8, one call). Trade only if one candidate gets >= 0.85 probability on
(decisive + toward) in one direction AND >= 0.30 on decisive. Among qualifiers, pick the most room to profit, where
room is 1 minus the entry price on the signalled side. A qualifier whose signalled side already trades at or above 95
cents (5 cents of room or less) is dropped before ranking; if
every qualifier is priced like that the decision is logged as `PASS priced_in` (no trade, market still tracked for
calibration). The same `priced_in` reason also comes from the freshness guard below, with a `BUY_*` action instead of
`PASS`.
Then the freshness guards: no trade if the news was published > 10 min before we saw it, or if the Kalshi price
already moved >= 3c our way since publication. Size = `PAPER_MAX_TRADE_PCT` of bankroll, never paying more than
best ask + 3 cents or above 95 cents. One position per market, daily loss halt at `PAPER_DAILY_LOSS_HALT_PCT`.
Then the cost filter: no trade if the live book's spread on the held side is over `MAX_SPREAD_CENTS` (3c) or if the
round-trip cost (that spread plus the entry and exit Kalshi taker fees per contract, 0 on Polymarket) is more than
`COST_TO_ROOM_MAX` (25%) of the room to profit, logged as `PASS too_expensive`. The 0.85 / 0.30 thresholds are
unchanged. Room in the cost filter comes from the live book (1 minus best ask), not the cached quote used for ranking.
The same filter blocks `no_exit_liquidity` (the held side has no bid, so the position could never be sold) and
`longshot` (entry below `MIN_ENTRY_PRICE`, 3c). All three are recorded as `PASS`. Markets whose title or ticker match
`MOVE_DENY_RE` (gas price and other price-on-a-date ladders) never fire the move detector, and a move event never
matches markets of its own series family.
The filter runs after selection, so when the chosen market is too expensive a cheaper second qualifier is not picked
(the market is still tracked).

## Real trading (opt-in)

Every install starts on paper, and every engine start resets to paper. To allow real Kalshi orders:

1. Create a Kalshi API key **with trading permission** and set `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`.
2. Set `LIVE_TRADING_ENABLED=1` (and, if you want, tighter `LIVE_MAX_*` caps) in `.env`, then start the engine.
3. In the dashboard's TRADING MODE panel press SWITCH TO LIVE MONEY, read the terms, type `TRADE REAL MONEY`, press ARM LIVE MONEY. The masthead badge turns to LIVE MONEY.

From then on, every trade the engine takes on a Kalshi market is also sent to Kalshi as a real order, whatever
triggered it: RSS and SEC headlines, X and Bluesky posts, the Trump archive feed, and Kalshi price-move events all
count. Turn off any source you do not want trading real money (`XAI_API_KEY` empty, `BSKY_ENABLED=false`) before
flipping the switch.

- An immediate-or-cancel limit buy at the same limit the paper fill used (best ask + 3c, never above 95c), so nothing
  rests on the book. Sized so price plus the worst-case taker fee fits `LIVE_MAX_ORDER_USD`.
- Hard caps: `LIVE_MAX_ORDER_USD` per order, `LIVE_MAX_DAILY_USD` per 24 h, `LIVE_MAX_ORDERS_PER_HOUR`, and one real
  position per market. An order whose outcome is unknown (network timeout) counts at full size and is never retried.
- **It only buys.** It never sells or closes; positions are held to settlement unless you close them on kalshi.com.
  Kalshi nets YES and NO in the same market, so a bot NO buy offsets a YES position you opened by hand there (and
  vice versa). Trade real money by hand in other markets, or keep a separate Kalshi account for the bot.
- Caps are counted from the local ledger. Restoring a backup carries the real-order rows over from the current ledger;
  they are lost only if the current ledger itself is gone (deleted or corrupted), in which case reconcile on kalshi.com before arming. One engine per results folder: a second one refuses to start.
- Kalshi only. Polymarket, shadow and test (`--inject`) trades always stay on paper. The paper book keeps recording
  every trade either way, and real orders show in their own table on the dashboard (`live_orders` in the ledger).
- Back to paper by itself on a restart, a Kalshi auth error, or three failed orders in a row. Back to paper by hand
  with the dashboard button or `python3 -m fastlane.live paper`. Last resort: revoke the API key on kalshi.com.

The switch only works from the dashboard on your own machine: it needs a per-process token that other websites cannot
read (true for browsers only; the API also hands the token and accepts arming only to loopback clients unless `LIVE_ALLOW_REMOTE_CONTROL=1`), and the Vercel copy has no switch at all. Every write goes through one guard (`X-Fastlane-Control: 1`, JSON only,
Host allow-list, no CORS, 404 on the hosted copy). Back to paper: BACK TO PAPER in the same panel or
`python3 -m fastlane.live paper`.

Never expose the API port beyond this machine (`--host 0.0.0.0` without the `127.0.0.1:` Docker mapping, port forwarding, tunnels) while `LIVE_TRADING_ENABLED=1`: the Host check only protects browsers and does not stop other machines, and anyone who can reach the port can arm real trading within the `LIVE_MAX_*` caps.

### Verifying NO orders on the demo exchange

Real NO orders stay off until a demo run proves that the order the bot sends opens a NO position. The bot buys NO by
sending a Create Order (V2) `ask` on the YES book (selling YES at 1 minus the NO limit). Whether that opens a NO
position when you hold no YES can only be seen by filling one and reading the position. Demo uses its own account and
API key (create it at demo.kalshi.co; a production key does not work there) and fake money. Never point this at
production.

Demo data is kept apart from production: with `KALSHI_BASE_URL=https://demo-api.kalshi.co` the engine, API, backups
and dashboard use `fastlane/results-demo/` (ledger, backups, market cache, mode and engine files), never
`fastlane/results/`. Both folders are gitignored. Run the API with the same `KALSHI_BASE_URL` to see the demo data; the
masthead reads `DEMO` (not `LIVE MONEY`) when armed against the demo host.

1. The one-off script `tools/demo_no_order_check.py`. It refuses to run unless the base URL is exactly the demo host,
   sends nothing without `--yes`, and is not part of the Docker image. `--fill` places ONE 1-contract NO buy at a
   marketable price (100 minus the best YES bid, so it takes that bid), then calls the signed
   `GET /trade-api/v2/portfolio/positions?ticker=...` and passes only if the position is +1 NO (`-1` in the signed
   `position` field, which counts YES positive and NO negative). It prints the raw JSON either way. `--close` buys the
   one contract back afterwards.

```bash
export KALSHI_BASE_URL=https://demo-api.kalshi.co      # plus a DEMO KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH
python3 tools/demo_no_order_check.py --ticker <liquid demo ticker> --fill                       # dry run
python3 tools/demo_no_order_check.py --ticker <liquid demo ticker> --fill --yes --close \
  --save tests/fixtures/demo_no_fill.json
```

   Without `--fill` the script runs the accept-only mode: one NO buy at 1 to 5 cents. A NO limit of p cents is a YES
   ask at 100 - p cents and matches any YES bid at or above that, so this only fills against a YES bid of 95 cents or
   more and is cancelled if unmatched (the order is immediate_or_cancel, so it never rests). It proves the body is accepted (HTTP 2xx) and nothing else.
2. Or run the engine against demo (`KALSHI_BASE_URL`, `LIVE_TRADING_ENABLED=1`, `LIVE_ALLOW_NO_SIDE=1`,
   `LIVE_MAX_ORDER_USD=1`), arm from the dashboard, and wait for a NO signal; then read the demo positions yourself.

Setting `LIVE_ALLOW_NO_SIDE=1` in your real `.env` requires a passing `--fill` check (`PASS: position is +1 NO`). Commit
the saved response as a test fixture only if it came from a real demo run; do not write one by hand.

## Dashboard on Vercel

The engine runs on your machine; Vercel cannot hold the news pollers or the Kalshi WebSocket. What you can put on
Vercel is a password-protected, read-only copy of your dashboard, on your own Vercel account:

```bash
npm i -g vercel && vercel login         # once
python3 -m fastlane.deploy              # prints your URL and a generated password (stored in fastlane/results/vercel.json)
python3 -m fastlane.run --vercel        # engine + resync every 30 min when the ledger changed
```

The deployment holds a snapshot of your ledger, the markets it mentions, and a salted hash of the password (never the
password, never your `.env`). Pages are `noindex`. Set your own name or password with `--name` / `--password`. Vercel's
free plan allows 100 deployments a day, so syncing is limited to every 20 minutes or slower. The copy is read-only: no
shadow toggle, no real-money switch; the real-orders table is shown from the snapshot.

## Error reports

Every install writes errors to `fastlane/results/errors.log`. If you opt in (see below), crash reports also go to a Sentry project; the engine's first line says which applies. Each report holds the exception type, a scrubbed message, stack
frames (file, line, function, with your home directory replaced by `~`), the fastlane version, Python version and OS.
It never holds API keys, environment variables, local variables, headlines, request data, IP addresses or your
hostname, and the same error is sent at most once per 10 minutes (30 an hour at most). The engine prints which mode is
active on start. Reports are **opt-in**: nothing is sent unless you set your own `FASTLANE_SENTRY_DSN` (to your own Sentry) or
`FASTLANE_TELEMETRY=1` (share with the maintainer, only if the build has a DSN).

## Rate limits and spending caps

- Dashboard API: `API_RATE_LIMIT_PER_MIN` per client (default 120, HTTP 429 with `Retry-After` beyond). The paper/real
  switch has its own bucket of 10 a minute. The Vercel copy limits password attempts the same way.
- Jev: at most `JEV_MAX_CALLS_PER_HOUR` calls and `JEV_MAX_USD_PER_DAY` dollars (OpenRouter's reported cost), counted
  from the ledger so a restart does not reset them. Also set a credit limit on the key itself at openrouter.ai.
- Real orders: the `LIVE_MAX_*` caps above.

## Backups

The engine backs the ledger up every `BACKUP_EVERY_HOURS` and on clean shutdown: a consistent SQLite snapshot,
gzipped, with a manifest of row counts, in `BACKUP_DIR` (keep `BACKUP_KEEP`). `python3 -m fastlane.backup --verify`
is the restore drill: it restores the newest backup to a temp file, checks integrity, checks the counts match the
manifest, and runs the dashboard query on it. Run it after setting things up, and after changing `BACKUP_DIR`.
`--restore PATH --yes` replaces the ledger (stop the engine first, it refuses while one runs; the old one is kept as `ledger.db.pre-restore-*`). Real-order rows are carried over from the current ledger, so a restore never resets the real-money caps.
Under Docker the backups land in the results volume; mount a second volume at `BACKUP_DIR` to keep them apart. With the demo host, backups go to `BACKUP_DIR/demo`.

If a release goes wrong, see [ROLLBACK.md](ROLLBACK.md).

## Shadow mode

Why: about three hours of live paper trading produced zero fills. A calibration on 35 live decisions suggested a
looser rule would have lost money after spread (strong leans blocked only for "not decisive" averaged -7.2c per
contract), yet many "signal too weak" rows turn green later. The sample is too small to act on, so the real
thresholds stay put and a shadow book measures the alternative.

What: the same Jev answers, the same scoring, room ranking and no-room fallback, with looser thresholds
(`SHADOW_SIGNAL_THRESHOLD`, `SHADOW_DECISIVE_MIN`). It is evaluated only when the real `decide()` returned `PASS`; a
BUY later turned into a PASS by `no_book`, `unknown_market`, `too_expensive`, a freshness guard or a risk check does not
count, and the decision row records `real_signalled`. A NULL `shadow_reason` means "not evaluated" (synthetic event,
shadow disabled, or the pass was cancelled at shutdown). It uses the
same freshness guards, cost filter, fill simulation, fee and sizing, has its own one-position-per-market rule, never
counts toward the bankroll, the daily loss halt or the real `already_in_market`, and never runs on synthetic
(`--inject`) events. Paper only: the shadow book places no orders either. A shadow signal threshold below 0.30 does
nothing, because `irrelevant` fires first.

Strength buckets: shadow P&L is split by signal strength (`0.60-0.70`, `0.70-0.85`, `0.85+`) so one shadow book shows
where the profitable cutoff is. The `0.85+` bucket is lean-only by construction, because a decisive 0.85+ signal is a
real trade. Shadow never runs when the real rule signalled a BUY, even if that BUY was later turned into a PASS
(`no_book`, `unknown_market`, `too_expensive`): those events are recorded as `real_signalled`.

Storage: `trades.shadow = 1` with `signal_strength` and `signal_decisive`; marks live under `shadow:<event_id>` because
the real `PASS` decision also tracks its own market under the plain event id, and the shadow rule may pick a different
market on the same event.

The console line for a shadow fill names the shadow market, which can differ from the market the real decision considered.

### Turning shadow mode on and off

- Press `H` on the dashboard (or click the `SHDW` cap). The masthead `SHADOW` cell shows ON (blue) or OFF.
- Off stops new shadow evaluations only. Shadow trades already in the ledger stay visible, keep being marked, and
  still count in the Shadow book and report.
- The setting is stored in the ledger `settings` table and wins over `SHADOW_ENABLED`; the engine re-reads it every
  2 s, no restart. Delete the row to fall back to the env variable:
  `sqlite3 fastlane/results/ledger.db "DELETE FROM settings WHERE key='shadow_enabled'"`.
- The route is `POST /settings/shadow` with body `{"enabled": true|false}` and headers `Content-Type:
  application/json` and `X-Fastlane-Control: 1`. It is one of the API's two writes (the other is the paper/real switch) and can only toggle shadow mode. On the Vercel copy the route returns 404 and the `H` key is hidden. The
  custom header plus the JSON content type mean a cross-site page would need a CORS preflight, which the API does not
  answer; the Host allow-list (`FASTLANE_ALLOWED_HOSTS`) still applies. There is no authentication beyond the
  Host and header checks, so anyone who can reach an allowed Host (for example a LAN name added to
  `FASTLANE_ALLOWED_HOSTS`) can flip the switch.

  ```bash
  curl -s -X POST localhost:8787/settings/shadow -H 'Content-Type: application/json' \
    -H 'X-Fastlane-Control: 1' -d '{"enabled": false}'
  ```
- Paper only, as everything else: the switch starts or stops a paper experiment; nothing can be sent to an exchange.

Where to see it: the dashboard key `3` (SHADOW BY SIGNAL), `/trades?book=shadow`, and the "Shadow vs real"
section of `python3 -m fastlane.report`.

How to act on it: loosen the real thresholds only if a bucket is profitable after costs over a meaningful sample.

Also: the first poll of every feed is backlog and is never traded. The Kalshi taker fee is
`0.07 * n * p * (1-p)`, rounded up to the next cent. Price marks are recorded at +5s, 30s, 60s, 5m, 15m and 1h.

## Measured performance (measured on one setup, yours will differ)

- Jev decision p50: ~375 ms.
- End to end, headline to paper fill: 340-440 ms.
- Market matcher: < 1 ms over ~31k markets.

Running in a US-East region cuts round trips to the US-hosted APIs (OpenRouter, Kalshi, Polymarket, SEC, the news
CDNs). Numbers above come from one setup.

Speed notes:

- Connections are kept warm every 3 s (upstreams drop idle connections after ~5 s; a cold call costs ~400 ms more).
- Every candidate's order book is fetched while Jev is deciding, so the fill never waits.
- Feeds are polled at their CDN refresh rate (Cache-Control max-age); faster returns identical bytes.
- Feed parsing runs off the event loop; failing feeds back off exponentially.

## Limitations

- RSS lags the first X post by a median of about half an hour on the stories we measured; X via xAI costs money and returns at most about 10 posts per call, so a very busy minute can be truncated; Bluesky newsrooms post a subset of their wire output.
- Jev (TypeSafe, via the OpenRouter Decisions API, early access) may change or disappear, and vendor latency claims
  are unverified.
- The Kalshi tape needs an API key. Polymarket has no live tape here.
- Paper fills assume the book you fetched is the book you would have hit. Real orders can fill worse, or not at all.
- Real trading only opens positions. There is no exit logic: positions are held to settlement or closed by you.
- There is no resolution tracking: P&L is mark-to-bid.

## Outputs

`fastlane/results/` (gitignored; `fastlane/results-demo/` when `KALSHI_BASE_URL` is the demo host): `ledger.db` (SQLite), `universe.json` (market cache, refreshed every 15 min),
`backups/`, `errors.log`, `trading_mode.json` and `engine_state.json` (the paper/real switch), `vercel.json` and
`vercel/` (the deploy), and benchmark files.

The decision log shows two numbers per judged headline: `price moved` (mid-price move in the leaned direction, before
costs) and `after costs` (per contract, bought at the ask at decision, sold at the bid at the latest mark, minus taker
fees). The second is the honest one.

## Contributing and license

Run `python3 -m pytest -q` before sending changes. Licensed under AGPL-3.0, see `LICENSE`.
