# Changelog

## Unreleased

### Added
- Real trading on Kalshi, opt-in (`fastlane/live.py`). Off unless `LIVE_TRADING_ENABLED=1` and the dashboard switch is
  flipped with the typed confirmation `TRADE REAL MONEY`; every engine start resets to paper. Immediate-or-cancel
  buys only (Create Order V2), at the paper fill's limit, capped by `LIVE_MAX_ORDER_USD`, `LIVE_MAX_DAILY_USD`,
  `LIVE_MAX_ORDERS_PER_HOUR` and one position per market. Trips back to paper on a restart, an auth error or three
  failed orders. Kill switch: `python3 -m fastlane.live paper`. Ledger table `live_orders`.
- API: `GET /control/state` and `POST /control/mode` (per-process token, same-origin, rate limited). Dashboard mode
  bar, confirmation panel, REAL MONEY banner and a real-orders table.
- Rate limits: per-client token bucket on the API (`API_RATE_LIMIT_PER_MIN`, HTTP 429 + `Retry-After`) and a Jev
  budget (`JEV_MAX_CALLS_PER_HOUR`, `JEV_MAX_USD_PER_DAY`; decisions logged as `PASS jev_hourly_cap` /
  `jev_daily_spend_cap`).
- Error reports (`fastlane/errors.py`): `fastlane/results/errors.log` on every install, plus scrubbed crash reports
  to the maintainer's Sentry (opt out `FASTLANE_TELEMETRY=0`, or your own `FASTLANE_SENTRY_DSN`). New dependency
  `sentry-sdk`.
- Backups (`fastlane/backup.py`): scheduled (`BACKUP_EVERY_HOURS`) and on shutdown, gzipped with a row-count manifest;
  `--verify` restore drill, `--restore`, pruning (`BACKUP_KEEP`), off-machine `BACKUP_DIR`.
- Vercel (`fastlane/deploy.py`, `fastlane/hosted.py`): `python3 -m fastlane.deploy` puts a password-protected,
  read-only copy of the dashboard on the user's own Vercel account; `fastlane.run --vercel` keeps it in sync.
- `ROLLBACK.md`: the launch-day rollback plan.

### Changed
- API errors return `{"error": "internal"}` with no stack trace; `/docs` and `/openapi.json` are off.
- `tests/test_no_orders.py` now allows exactly one order endpoint, in `fastlane/live.py`, and still forbids cancel,
  amend, batch and sell-to-close code everywhere.

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
