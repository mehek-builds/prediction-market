# Changelog

## 0.4.0 - 2026-10-09

### Added
- Dashboard rebuilt as a terminal screen: TERMINAL (dark) and LEDGER (light) themes following `prefers-color-scheme`
  with a persisted toggle; command bar with keyboard shortcuts; sortable trade blotter with a card view; 24h P&L
  curve per book; latency monitor (match, Jev, book, total; p50/p90); news/decision tape; shadow-by-signal table;
  masthead cells for the tape and the feeds (X calls and spend against the daily budget, Bluesky mode, sources active
  in the last hour); readable labels for `no_exit_liquidity`, `longshot` and `too_expensive`; 375px layout;
  `prefers-reduced-motion` honoured. Still one static file with no external requests.
- Shadow mode can be switched on and off at runtime from the dashboard (key `H`, masthead `SHADOW`). The setting
  lives in a new ledger `settings` table and wins over `SHADOW_ENABLED`; the engine picks it up within 2 s, no
  restart. Off stops new shadow evaluations only; existing shadow trades stay and keep being marked.
- API: `GET /status` (ledger-derived liveness: last event, decision, trade and tick timestamps, 1h counts, per-source
  activity, plus the `x` and `bluesky` blocks of `/health`); `GET /settings`; `POST /settings/shadow` (the only
  non-GET route: JSON body `{"enabled": bool}`, requires `X-Fastlane-Control: 1` and `Content-Type:
  application/json`, same Host allow-list, no CORS); `/decisions` rows gain `shortlist_ms`, `book_ms`, `n_candidates`.
- Ledger: `settings` table (key/value, idempotent), `ticks_ts` index.

### Changed
- `Engine.shadow_enabled` is now read from the ledger setting (falling back to `SHADOW_ENABLED`) with a 2 s cache,
  instead of once at start. The status line shows `shadow mode on|off`.
- The engine writes `{"enabled": false}` to the X feed status when the X feed is off, so `/health` cannot stay at
  `enabled: true` from an earlier run.
- The X feed counts timeouts and dropped connections in its `errors` counter.
- Bluesky poll fallback now reports connected (`bsky poll up`) while polls succeed; reconnects alternate between the
  jetstream2 and jetstream1 hosts and the log line includes the close code and reason.
- Polymarket sports markets (spreads, moneylines, totals, game lines) are excluded from the universe, including from
  an older cached `universe.json`.
- `tests/test_no_orders.py` allows exactly one POST route in `api.py` (`/settings/shadow`).

## 0.3.0 - 2026-10-09

### Added
- X source: one Grok `x_search` call per poll over a fixed group of up to 10 handles (`XAI_API_KEY`, `XAI_X_HANDLES`,
  `XAI_POLL_SECONDS`), only inside an active window (`XAI_WINDOW_DAYS/HOURS/TZ`, default US market hours) and under a
  hard daily budget (`XAI_DAILY_BUDGET_USD`, default 25, persisted in the ledger). Posts are accepted only when the
  URL handle is in the allowed list, the snowflake id decodes to a time inside the poll window, and the id is new;
  the first poll of each window is backlog. Events are `x:<handle>` with the exact post time.
- Bluesky source: newsroom accounts over the Jetstream firehose (keyless, `BSKY_ENABLED`, `BSKY_HANDLES`), own
  top-level posts only, with a getAuthorFeed polling fallback. Events are `bsky:<handle>` with createdAt clamped to
  receipt time.
- Unofficial Trump archive RSS (`trumpstruth`) polled every 10 s; `@truthsocial` in the default X group.
- Ledger tables `x_spend` (calls, USD, posts per UTC day) and `feed_status`; `/health` reports X calls and spend
  today, budget and window flags, and Bluesky connection state; the status line shows the same.
- Cost filter: `no_exit_liquidity` (held side has no bid) and `longshot` (entry below `MIN_ENTRY_PRICE`, default
  0.03) block real and shadow trades, recorded as `PASS`.
- Move detector: `MOVE_DENY_RE` (default: gas price and price-on-a-date ladders, `KXAAAGAS` tickers) never fires, and
  a move event no longer matches markets of its own series family.

### Changed
- The shadow fill console line names the shadow market (venue and question), which can differ from the real
  decision's market.
- `decision.cost_settings()` returns `(max_spread, cost_to_room_max, min_entry)`.
- `books.cost_block()` takes `min_entry` and returns `no_exit_liquidity` / `longshot` where it used to abstain.
- Move events carry `exclude_series` instead of `exclude_event`.
- `tests/test_no_orders.py` allows exactly two outbound POSTs: Jev and xAI. Real-rule thresholds unchanged.

## 0.2.0 - 2026-10-09

### Added
- Cost filter (real rule and shadow): `PASS too_expensive` when the live book's held-side spread is over
  `MAX_SPREAD_CENTS` (default 3) or the round-trip cost (spread plus entry and exit Kalshi taker fees per contract)
  exceeds `COST_TO_ROOM_MAX` (default 0.25) of the room to profit. Applied after selection; thresholds unchanged.
- Shadow mode: a separate paper book recording what a looser rule (`SHADOW_SIGNAL_THRESHOLD`, default 0.60;
  `SHADOW_DECISIVE_MIN`, default 0.0; `SHADOW_ENABLED`) would have traded on the same Jev answers, only when the real
  rule had no buy intent. Same freshness guards, cost filter, fill simulation, fee and sizing; own
  one-position-per-market rule; never counts toward bankroll, the daily loss halt or real positions. P&L is broken
  down by signal strength bucket (0.60-0.70, 0.70-0.85, 0.85+) in the report and the dashboard.
- Ledger: `trades.shadow`, `trades.signal_strength`, `trades.signal_decisive`,
  `decisions.shadow_action/shadow_reason/shadow_market_id` (old ledgers migrate on open); shadow marks keyed
  `shadow:<event_id>`.
- API: `shadow`, `signal_strength`, `bucket` per trade; `/trades?book=all|live|shadow|test`; `summary.shadow_trades`
  and `summary.books` (shadow with `buckets`); `/decisions` gains shadow fields and `after_costs_cents`.
- Dashboard: All / Live / Shadow / Test tabs, SHADOW-tagged cards, bucket strip on the Shadow view, "Shadow bought
  YES/NO" chip, and the decision log now shows "price moved" (mid, before costs) and "after costs" (bid exit, fees).
- Report: "Shadow vs real" section by strength bucket (count, avg P&L per contract after spread and fees per
  horizon, winners/losers).

### Changed
- `decision.decide()` takes `signal_threshold` / `decisive_min` keyword parameters (defaults are the real rule, which
  is unchanged).

## 0.1.1 - 2026-10-09

### Changed
- `decision.decide()` drops qualifying candidates whose signalled side already trades at or above `books.MAX_ENTRY_PRICE`
  (95c, 5c of room or less). If every qualifier is priced like that the decision is `PASS priced_in` and the market is
  still tracked; previously such a market could be chosen and then logged as `no_fill_within_limit` by the fill guard.
- README "Trade rule and guards" documents the no-room fallback.

## 0.1.0 - 2026-10-09

- Initial public release.
