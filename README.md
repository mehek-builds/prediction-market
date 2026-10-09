# prediction-market

A paper-only, news-driven trading research bot for Kalshi and Polymarket. It reads headlines, matches them to open
markets, asks a decision model one typed question per candidate market, applies a fixed trade rule, and simulates a
fill against the live order book. Nothing is ever sent to an exchange: there is no order-placing code anywhere in
this repository, and a test enforces that.

> **Disclaimer.** This is educational and research software. It trades on paper only, forever. It is not financial
> advice. It comes with no warranty (see AGPL-3.0 sections 15 and 16). You are responsible for checking the Kalshi
> and Polymarket terms of service and your own eligibility in your jurisdiction. Do not use VPNs or any other means
> to evade geo-restrictions. Prediction markets may be restricted or illegal where you live.

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
| `fastlane/api.py` | Read-only FastAPI over the ledger (`GET` routes), plus one control route: `POST /settings/shadow` toggles shadow mode. |
| `fastlane/static/index.html` | Single-file terminal dashboard served at `/` (light and dark, keyboard driven), no external requests. |

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
| `FASTLANE_ALLOWED_HOSTS` | optional | Extra Host header values the dashboard API accepts (comma separated). `localhost` and `127.0.0.1` are always allowed; other Hosts get HTTP 400 (DNS rebinding guard). |

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
```

Docker (the Dockerfile header has the same commands):

```bash
docker build -t fastlane .
docker run -d --restart unless-stopped --env-file .env -v fastlane-results:/app/fastlane/results fastlane
docker run -p 127.0.0.1:8787:8787 --env-file .env -v fastlane-results:/app/fastlane/results fastlane \
  python3 -m uvicorn fastlane.api:app --host 0.0.0.0 --port 8787
```

`--env-file` cannot hold multi-line values, so put the Kalshi key body on one line or mount a key file.

## Dashboard

`python3 -m uvicorn fastlane.api:app --port 8787` serves one static page at `/`: no build step, no external requests.

- Masthead: a persistent `PAPER` badge and status cells for the run state, the Kalshi tape, the feeds (X calls and
  spend against the daily budget, Bluesky websocket or polling, feeds with events in the last hour), the shadow switch
  and data age, plus ET and UTC clocks.
- BOOKS: LIVE, SHADOW and TEST rows with a totals row for the real book.
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
the feeds are the bottleneck. Three faster sources feed the same event queue. Paper only: nothing here places orders,
and post text is treated as untrusted data (it is stored and shown as a headline, never used as an instruction or a
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
  application/json` and `X-Fastlane-Control: 1`. It is the API's only write and can only toggle shadow mode. The
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
- Paper fills assume the book you fetched is the book you would have hit.
- There is no resolution tracking: P&L is mark-to-bid.

## Outputs

`fastlane/results/` (gitignored): `ledger.db` (SQLite), `universe.json` (market cache, refreshed every 15 min), and
benchmark files.

The decision log shows two numbers per judged headline: `price moved` (mid-price move in the leaned direction, before
costs) and `after costs` (per contract, bought at the ask at decision, sold at the bid at the latest mark, minus taker
fees). The second is the honest one.

## Contributing and license

Run `python3 -m pytest -q` before sending changes. Licensed under AGPL-3.0, see `LICENSE`.
